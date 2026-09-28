"""TTS 缓存 —— 相同文本只合成一次。

收益：
  - 状态机的固定引导语启动时后台预合成 → 播放几乎无延迟
  - 重复回复（如"好的，再见"）不再重复打云端接口（每句约 1~2 秒）
  - 唤醒应答「我在」预合成后是纯内存读取，所以唤醒依旧是秒回

另外这里负责**唤醒应答这一类短句的"挑一条好的"**（见 prewarm_ack）：
qwen3-tts-flash 对两三个字的输入会"赶着收尾"，同一句话的时长能在 320ms~960ms
之间乱跳，偶尔在 85% 处直接掐断（听感就是"我在……后面卡了半个字"）。
唤醒应答每次唤醒都要播，必须挑干净了再缓存，否则用户每次都听到那句残句。
"""
from __future__ import annotations

import hashlib
import io
import threading
import wave
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .. import config

_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_MAX_ITEMS = 120


def _current_rate() -> int:
    """语速参与缓存键：改了语速后不会重放旧语速的音频。"""
    try:
        from ..core.interaction_config import speech_rate_percent
        return int(speech_rate_percent())
    except Exception:
        return 0


def _key(text: str, voice: str, rate: int) -> str:
    return hashlib.sha1(f"{voice}\x00{rate}\x00{text}".encode("utf-8")).hexdigest()


def get(text: str, voice: str, rate: int | None = None) -> bytes | None:
    k = _key(text, voice, _current_rate() if rate is None else rate)
    with _LOCK:
        data = _CACHE.get(k)
        if data is not None:
            _CACHE.move_to_end(k)
        return data


def put(text: str, voice: str, data: bytes, rate: int | None = None) -> None:
    if not text or not data:
        return
    k = _key(text, voice, _current_rate() if rate is None else rate)
    with _LOCK:
        _CACHE[k] = data
        _CACHE.move_to_end(k)
        while len(_CACHE) > _MAX_ITEMS:
            _CACHE.popitem(last=False)


def size() -> int:
    with _LOCK:
        return len(_CACHE)


def _default_voice() -> str:
    """缓存键里的"音色"：由当前 TTS 引擎决定（现在是 Qwen3-TTS 的 模型:音色）。

    预热与查询必须用同一个键，否则会出现"预热了却读不到"——
    换 TTS 引擎后就踩过：预热写的是旧引擎的音色名，对话查的是新引擎的键，
    结果即时回应一直不播。
    """
    try:
        from .tts import cache_voice
        return cache_voice()
    except Exception:
        return "qwen3-tts"


def prewarm(prompts: list[str], voice: str | None = None, gap_sec: float = 0.25) -> int:
    """后台预合成一批固定文案（状态机引导语），返回成功条数。

    逐条串行并留出间隔：并发打云接口会触发限流，反而拖慢真实请求。
    （发声是云端 Qwen3-TTS，单句约 1~2 秒，间隔留够。）
    """
    import time as _time

    from .tts import synthesize

    voice = voice or _default_voice()
    ok = 0
    tmpdir = config.PATHS.media_dir / "audio"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(prompts):
        if i:
            _time.sleep(gap_sec)
        if get(text, voice):
            continue
        out = tmpdir / f"prewarm-{i}.wav"
        try:
            if synthesize(text, out) and out.exists():
                put(text, voice, out.read_bytes())
                ok += 1
        except Exception:
            pass
        finally:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
    return ok


# ===================== 唤醒应答：挑一条"收干净了"的 =====================
# 「我在」这类两三字短句的正经时长大约 500~700ms：
ACK_TARGET_MS = 620.0     # 期望时长（挑样本时用它排序）
ACK_MIN_MS = 380.0        # 短于这个时长 = 尾音被掐掉了
ACK_MAX_MS = 1800.0       # 长于这个 = 拖沓/多念了一段
ACK_TAIL_MS = 60          # 句尾至少要有这么长的静音
ACK_TAIL_RMS = 0.01       # 句尾静音的能量上限（超过说明还在出声）
ACK_TRIES = 3             # 最多试合成几次


def wav_stats(data: bytes) -> dict | None:
    """解析 WAV 的时长 / 句尾静音能量 / 峰值；解析不了返回 None。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate, frames, channels, width = (w.getframerate(), w.getnframes(),
                                             w.getnchannels(), w.getsampwidth())
            raw = w.readframes(frames)
        if not rate or not raw or width not in (1, 2, 4) or channels < 1:
            return None
        dtype = {1: "u1", 2: "<i2", 4: "<i4"}[width]
        pcm = np.frombuffer(raw, dtype=dtype).astype(np.float64)
        pcm = pcm / (32768.0 if width == 2 else 2147483648.0 if width == 4 else 128.0)
        tail = pcm[-max(1, int(rate * channels * ACK_TAIL_MS / 1000)):]
        return {
            "ms": 1000.0 * frames / rate,
            "peak": float(np.max(np.abs(pcm))) if pcm.size else 0.0,
            "tail_rms": float(np.sqrt(np.mean(tail ** 2))) if tail.size else 0.0,
        }
    except Exception:
        return None


def _ack_ok(stats: dict | None) -> bool:
    """这条合成结果算不算"一句收干净的应答"。"""
    if not stats:
        return False
    return (ACK_MIN_MS <= stats["ms"] <= ACK_MAX_MS
            and stats["peak"] > 0.02
            and stats["tail_rms"] <= ACK_TAIL_RMS)


def _ack_score(stats: dict | None) -> tuple:
    """排序用：先要"收干净"，再要"时长接近期望"，最后比时长。"""
    if not stats:
        return (2, 1e9, 1e9)
    return (0 if _ack_ok(stats) else 1, round(stats["tail_rms"], 4),
            abs(stats["ms"] - ACK_TARGET_MS))


def prewarm_ack(text: str, voice: str | None = None, tries: int = ACK_TRIES) -> bytes | None:
    """合成唤醒应答并缓存其中最干净的一条，返回音频字节（失败返回 None）。

    命中缓存直接返回，不重复打云端接口（唤醒必须秒回）。
    合成质量不达标（尾音被掐断/句尾还在出声）时会重试，最多 tries 次；
    都不达标就用其中最好的一条 —— 有声音总比唤醒后一声不响强。
    """
    import time as _time

    from .tts import synthesize

    text = str(text or "").strip()
    if not text:
        return None
    voice = voice or _default_voice()
    cached = get(text, voice)
    if cached:
        return cached

    tmpdir = config.PATHS.media_dir / "audio"
    tmpdir.mkdir(parents=True, exist_ok=True)
    best: bytes | None = None
    best_stats: dict | None = None
    for i in range(max(1, tries)):
        if i:
            _time.sleep(0.3)          # 连续打云接口容易被限流，留点间隔
        out = tmpdir / f"ack-{int(_time.time() * 1000)}-{i}.wav"
        try:
            if not synthesize(text, out) or not out.exists():
                continue
            data = out.read_bytes()
        except Exception:
            continue
        finally:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
        stats = wav_stats(data)
        if _ack_ok(stats):
            put(text, voice, data)
            print(f"[tts] 唤醒应答「{text}」合成 OK（{stats['ms']:.0f}ms，"
                  f"尾音 {stats['tail_rms']:.3f}，第 {i + 1} 次）", flush=True)
            return data
        if best is None or _ack_score(stats) < _ack_score(best_stats):
            best, best_stats = data, stats

    if best is None:
        return None
    put(text, voice, best)
    detail = (f"{best_stats['ms']:.0f}ms，尾音 {best_stats['tail_rms']:.3f}"
              if best_stats else "无法解析")
    print(f"[tts] 唤醒应答「{text}」{tries} 次都没达到理想质感（取最好的一条：{detail}）",
          flush=True)
    return best
