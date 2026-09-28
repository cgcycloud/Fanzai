"""Qwen3-TTS-Flash 云端合成（阿里云百炼 / DashScope）—— 项目唯一的发声引擎。

## 接口形态（阿里云百炼，非实时语音合成 Qwen-TTS）

    POST {base}/services/aigc/multimodal-generation/generation
    Authorization: Bearer $DASHSCOPE_API_KEY
    {
      "model": "qwen3-tts-flash",
      "input": {"text": "...", "voice": "Cherry", "language_type": "Chinese"}
    }

返回（非流式）：

    {"status_code": 200, "output": {"audio": {"url": "https://...wav", "data": ""}}}

**注意**：非流式输出里 `output.audio.data` 是**空串**，音频在 `output.audio.url`
（有效期 24 小时），必须再下载一次才能拿到字节。流式（`X-DashScope-SSE: enable`）
才是中间 chunk 给 base64 `data`、末 chunk 给完整 `url`。本项目按句合成、句子都短，
走非流式 + 下载更简单可靠。

文档：
  * API 参考 https://help.aliyun.com/zh/model-studio/qwen-tts-api
  * 使用指南 https://help.aliyun.com/zh/model-studio/non-realtime-tts-user-guide

## 为什么保留了熔断

云端合成依赖网络：单句通常 1 秒左右，但限流/抖动时可能拖到十几秒。连续失败
`FAILS_BEFORE_DOWN` 次后进入 `DOWN_SECONDS` 熔断期，`available()` 直接返回 False，
让上层**立刻**走本地提示音，而不是每句话都白等一次网络超时。

## 关于语速

qwen3-tts-flash 的 `input` 里**没有语速参数**（语速要靠 qwen3-tts-instruct-flash
的 `instructions` 指令控制）。所以设置面板里的「AI 语速」在本地做**时间轴重采样**：
变快=丢样本、变慢=插值，音高不变（见 `audio_time_stretch`）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import wave
from pathlib import Path

import requests

from .. import config

logger = logging.getLogger(__name__)

# ===================== 默认接口参数（可在 data_local/ai_config.json 覆盖）=====================
# 北京地域端点。新加坡地域的 Key 与端点不同，改了 base URL 记得同步换 Key。
DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_MODEL = "qwen3-tts-flash"
# 官方示例用的默认音色；其余可选音色见「Qwen-TTS 音色列表」文档。
DEFAULT_VOICE = "Cherry"
# 强制指定语种：官方文档明确"指定具体语种能显著提升合成质量，效果优于 Auto"。
DEFAULT_LANGUAGE = "Chinese"

# 熔断参数
DOWN_SECONDS = 120.0      # 连续失败后这段时间内不再尝试（直接走提示音）
FAILS_BEFORE_DOWN = 2     # 连续失败几次算"接口不可用"

_LOCK = threading.Lock()
_FAILS = 0
_DOWN_UNTIL = 0.0
_LAST_ERROR = ""
_LAST_MS = 0.0


# ===================== 熔断状态 =====================

def blocked() -> bool:
    """熔断中（最近的失败还没过期）。"""
    return time.time() < _DOWN_UNTIL


def note_success() -> None:
    global _FAILS, _DOWN_UNTIL, _LAST_ERROR
    with _LOCK:
        _FAILS = 0
        _DOWN_UNTIL = 0.0
        _LAST_ERROR = ""


def note_failure(reason: str) -> None:
    """记一次失败；连续失败到阈值就熔断（直接走提示音，不再每句等网络超时）。"""
    global _FAILS, _DOWN_UNTIL, _LAST_ERROR
    with _LOCK:
        _FAILS += 1
        _LAST_ERROR = reason
        if _FAILS >= FAILS_BEFORE_DOWN:
            _DOWN_UNTIL = time.time() + DOWN_SECONDS


def note_input_error(reason: str) -> None:
    """记一次"输入被拒"（HTTP 400）——**不熔断**。

    400 是"这段文本合不了"（实测纯表情符号/星号会返回 InvalidParameter），
    不是服务不可用；把它算进熔断会让"回复里带个表情"直接静音两分钟。
    """
    global _LAST_ERROR
    with _LOCK:
        _LAST_ERROR = reason


def reset_breaker() -> None:
    """手动清除熔断（保存新配置后调用，让用户不用等 2 分钟）。"""
    with _LOCK:
        global _FAILS, _DOWN_UNTIL, _LAST_ERROR
        _FAILS = 0
        _DOWN_UNTIL = 0.0
        _LAST_ERROR = ""


# ===================== 配置读取 =====================

def _settings() -> dict:
    """从加密配置里取 TTS 接口参数；读不到就退到本模块的默认常量。

    每次调用都读配置：用户在设置页改完 URL/Key 应当立即生效，不需要重启进程。
    （AIConfigStore.load 只读一个小 JSON，单句合成一次的开销可忽略。）
    """
    out = {
        "api_base": DEFAULT_API_BASE,
        "model": DEFAULT_MODEL,
        "voice": DEFAULT_VOICE,
        "language_type": DEFAULT_LANGUAGE,
        "api_key": "",
    }
    try:
        from ..core.ai_config import AIConfigStore
        cfg = AIConfigStore().load()
        if cfg is None:
            return out
        base = str(cfg.tts_api_url or "").strip()
        if base:
            out["api_base"] = base.rstrip("/")
        if cfg.tts_model:
            out["model"] = str(cfg.tts_model).strip()
        if cfg.tts_voice:
            out["voice"] = str(cfg.tts_voice).strip()
        if cfg.tts_language:
            out["language_type"] = str(cfg.tts_language).strip()
        # cfg.tts_api_key 已在 load() 里解析好（明文优先，兼容旧的加密字段）
        out["api_key"] = str(cfg.tts_api_key or "").strip()
    except Exception:
        pass
    if not out["api_key"]:
        # 环境变量兜底：方便在没有配置文件的环境里跑
        import os
        out["api_key"] = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    return out


def normalize_base(api_base: str) -> str:
    """把用户填的 Base URL 归一到 Qwen-TTS 真正需要的 base（`/api/v1`）。

    最常见的填错是**百炼的 OpenAI 兼容端点** —— 很多人手里就是这个 URL：

        https://dashscope.aliyuncs.com/compatible-mode/v1     ← 对话用的，TTS 不可用

    这个地址拼上 `/services/aigc/multimodal-generation/generation` 会得到 404，
    而且**响应体是空的**，报错完全看不出原因。同一个 host 上 TTS 的路径是固定的，
    所以这里直接认出来改写成 `/api/v1`，而不是让用户对着空 body 猜。

    其它 host（自建/代理）原样保留，不做猜测。
    """
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return DEFAULT_API_BASE
    # 已经是完整端点
    if base.endswith("/services/aigc/multimodal-generation/generation"):
        return base
    low = base.lower()
    if "dashscope.aliyuncs.com" in low:
        head = base.split("//", 1)
        host = head[1].split("/", 1)[0] if len(head) > 1 else head[0]
        # 兼容模式 / 只有裸 host / 只填到域名 → 一律归到官方 /api/v1
        if "/compatible-mode/" in low or low.endswith("/compatible-mode") or "/api/v1" not in low:
            return f"https://{host}/api/v1"
    return base


def _endpoint(api_base: str) -> str:
    """拼接合成端点：允许用户只填域名/`/api/v1`/完整 URL（见 normalize_base）。"""
    base = normalize_base(api_base)
    if base.endswith("/services/aigc/multimodal-generation/generation"):
        return base
    return f"{base}/services/aigc/multimodal-generation/generation"


def configured() -> bool:
    """有没有 Key（没配 Key 就不必尝试了）。"""
    return bool(_settings()["api_key"])


def available() -> bool:
    """现在能不能用：配了 Key 且不在熔断期。"""
    return configured() and not blocked()


def default_voice() -> str:
    return str(_settings()["voice"] or DEFAULT_VOICE)


def model_name() -> str:
    return str(_settings()["model"] or DEFAULT_MODEL)
def status() -> dict:
    return {
        "engine": "qwen3-tts",
        "available": available(),
        "configured": configured(),
        "model": model_name(),
        "voice": default_voice(),
        "api_base": str(_settings()["api_base"]),
        "blocked_sec": max(0.0, round(_DOWN_UNTIL - time.time(), 1)),
        "consecutive_failures": _FAILS,
        "last_error": _LAST_ERROR,
        "last_ms": round(_LAST_MS, 1),
    }


# ===================== 语速：时间轴重采样 =====================

def audio_time_stretch(pcm: bytes, sample_rate: int, n_channels: int, sampwidth: int,
                       speed: float) -> bytes:
    """整段 PCM 变速不变调（线性插值重采样）。

    speed>1 变快（句子更短）、speed<1 变慢。只改时间轴，不改音高也不做变调，
    对"把 AI 语速调慢一点"这个诉求足够用，且不引入额外的音频依赖。

    Args:
        pcm: 原始交错 PCM 字节。
        sample_rate: 采样率（本函数只用于计算输出长度）。
        n_channels: 声道数。
        sampwidth: 每样本字节数（本引擎是 16bit = 2）。
        speed: 目标倍速，1.0 为原样。
    """
    speed = float(speed or 1.0)
    frame_bytes = max(1, n_channels * sampwidth)
    if abs(speed - 1.0) < 1e-3 or not pcm:
        return pcm

    total_frames = len(pcm) // frame_bytes
    if total_frames < 2:
        return pcm
    out_frames = max(1, int(total_frames / speed))

    import numpy as np

    if sampwidth == 2:
        dtype = "<i2"
    elif sampwidth == 1:
        dtype = "u1"
    elif sampwidth == 4:
        dtype = "<i4"
    else:
        return pcm

    usable = total_frames * frame_bytes
    src = np.frombuffer(pcm[:usable], dtype=dtype).reshape(total_frames, n_channels)
    # 目标样本在源时间轴上的位置
    pos = np.arange(out_frames, dtype=np.float64) * (total_frames - 1) / max(out_frames - 1, 1)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, total_frames - 1)
    frac = (pos - lo).reshape(-1, 1)

    src_f = src.astype(np.float64)
    out = src_f[lo] * (1.0 - frac) + src_f[hi] * frac
    if sampwidth == 2:
        out = np.clip(np.round(out), -32768, 32767).astype("<i2")
    elif sampwidth == 4:
        out = np.clip(np.round(out), -2147483648, 2147483647).astype("<i4")
    else:
        out = np.clip(np.round(out), 0, 255).astype("u1")
    return out.tobytes()


# 能读出来的字符：中日韩汉字/假名/字母数字 + 常用中英文标点。
# 其余（emoji、☆★※、markdown 的 * # ` 等）**必须先删掉**：
# 实测把「😋」这种纯表情片段发给接口会返回 HTTP 400 InvalidParameter
# （"Due to invalid text, invalid audio was returned."），连续两次就熔断 120 秒
# —— 表现是"AI 回复念到一半就没声了，而且之后两分钟都不出声"。
_SPEAKABLE_RE = re.compile(
    r"[0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af，。！？；：、（）()《》〈〉「」『』“”‘’…—～,.!?;:'\"\-~ ]"
)


def sanitize_for_tts(text: str) -> str:
    """去掉念不出来的字符（emoji/符号），并压掉多余空白。"""
    kept = "".join(_SPEAKABLE_RE.findall(str(text or "")))
    return re.sub(r"\s{2,}", " ", kept).strip()


def _apply_speed(wav_bytes: bytes, speed: float) -> bytes:
    """对 WAV 字节做变速；解析失败时原样返回（绝不因为变速失败而丢声音）。"""
    if abs(float(speed or 1.0) - 1.0) < 1e-3:
        return wav_bytes
    import io
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            params = src.getparams()
            frames = src.readframes(params.nframes)
        if params.comptype != "NONE":
            return wav_bytes
        new_frames = audio_time_stretch(frames, params.framerate, params.nchannels,
                                        params.sampwidth, speed)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as dst:
            dst.setnchannels(params.nchannels)
            dst.setsampwidth(params.sampwidth)
            dst.setframerate(params.framerate)
            dst.writeframes(new_frames)
        return buf.getvalue()
    except Exception:
        return wav_bytes


# ===================== 合成 =====================

def _speed_from_rate(rate: str | int | float | None = None) -> float:
    """把设置面板的语速百分比（-50~50）换算成倍速（0.5~1.5，1.0 为原样）。

    沿用旧 edge-tts 的口径（`+15%` 就是快 15%），这样面板上已保存的数值
    语义不变，用户不用重新适应。
    """
    if isinstance(rate, (int, float)):
        pct = float(rate)
    elif isinstance(rate, str) and rate.strip().endswith("%"):
        try:
            pct = float(rate.strip().rstrip("%"))
        except ValueError:
            pct = 0.0
    else:
        try:
            from ..core.interaction_config import speech_rate_percent
            pct = float(speech_rate_percent())
        except Exception:
            pct = 0.0
    return max(0.5, min(2.0, 1.0 + pct / 100.0))


def normalize_wav_header(data: bytes) -> bytes:
    """把流式 WAV 的**占位长度**改写成真实长度（不改动音频数据）。

    实测 DashScope 的 Qwen3-TTS 返回的是"边生成边写头"的流式 WAV：RIFF size 写成
    `0x7FFFFEFF`（约 2GB 占位），data size 同理。一个 53KB 的 1.1 秒音频，头部自称
    12 小时：

        RIFF size = 2147483583   ← 实际只有 53796
        wave 模块读出 nframes = 1073741773 → 时长 44739 秒

    后果是浏览器算出的时长/进度条完全错乱，部分播放器还会直接拒播；本地的语速重采样
    也会拿到一个假的帧数。这里按真实字节数改写头部，幂等（已经正确就原样返回）。
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data

    import struct

    actual_riff = len(data) - 8
    out = bytearray(data)

    # RIFF size
    if struct.unpack_from("<I", out, 4)[0] != actual_riff:
        struct.pack_into("<I", out, 4, actual_riff)

    # 逐个 chunk 走，修正 data 的 size
    pos = 12
    while pos + 8 <= len(out):
        cid = bytes(out[pos:pos + 4])
        size = struct.unpack_from("<I", out, pos + 4)[0]
        body = pos + 8
        if cid == b"data":
            actual = len(out) - body
            if size != actual:
                struct.pack_into("<I", out, pos + 4, actual)
            break
        if size == 0 or body + size > len(out):
            break
        pos = body + size + (size & 1)

    return bytes(out)


def _download(url: str, timeout: float) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return normalize_wav_header(response.content)


# 429 = "Requests rate limit exceeded"：并发合成一轮回复的几句话时很容易撞上，
# 实测撞上后隔几十秒就恢复（不是账号没额度）。退避重试一次，
# 否则一句 429 记一次失败、两句就熔断 —— 表现是"回复念到一半没声了，之后整机静音 120 秒"。
RATE_LIMIT_RETRY_SLEEP = 1.2


def _post_with_retry(endpoint: str, headers: dict, payload: dict, limit: float):
    """POST 合成请求；撞上限流（429）时退避后重试一次。"""
    response = requests.post(endpoint, headers=headers, json=payload, timeout=(10, limit))
    if response.status_code == 429:
        logger.warning("Qwen-TTS 429 限流，%.1fs 后重试一次", RATE_LIMIT_RETRY_SLEEP)
        time.sleep(RATE_LIMIT_RETRY_SLEEP)
        response = requests.post(endpoint, headers=headers, json=payload, timeout=(10, limit))
    return response


def synthesize(text: str, output_path: Path, voice: str | None = None,
               rate: str | int | float | None = None,
               timeout: float | None = None) -> bool:
    """合成到文件（WAV）；成功返回 True，失败返回 False（上层走提示音）。

    一次调用可能发两个请求：① 合成（拿 URL）② 下载音频字节。
    """
    global _LAST_MS, _LAST_ERROR
    # 先清掉念不出来的字符（表情符号等）：直接发过去会被接口判为非法文本
    text = sanitize_for_tts(text)
    if not text:
        return False

    cfg = _settings()
    if not cfg["api_key"]:
        note_failure("未配置 TTS API Key")
        return False
    if blocked():
        return False

    limit = float(timeout if timeout is not None else getattr(config, "QWEN_TTS_TIMEOUT", 30.0))
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": cfg["model"],
        "input": {
            "text": text,
            "voice": (str(voice).strip() if voice else cfg["voice"]) or DEFAULT_VOICE,
            "language_type": cfg["language_type"] or DEFAULT_LANGUAGE,
        },
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    endpoint = _endpoint(cfg["api_base"])
    try:
        response = _post_with_retry(endpoint, headers, payload, limit)
        if response.status_code >= 400:
            # 百炼的 404 常常是**空响应体**，光看异常完全不知道哪儿错了；
            # 这里把状态码、实际 URL 和最常见的原因写进错误里，并**完整**记进日志
            # （日志不截断，便于排查；给用户看的 detail 才截断）。
            full = (response.text or "").strip()
            logger.error("Qwen-TTS HTTP %s @ %s body=%s",
                         response.status_code, endpoint, full[:1000])
            detail = full[:200]
            hint = ""
            if response.status_code == 404:
                hint = ("（端点不存在：音色合成的正确路径是 /api/v1/services/aigc/"
                        "multimodal-generation/generation；compatible-mode/v1 是对话端点，"
                        "TTS 不可用 —— Base URL 留空即用官方默认）")
            elif response.status_code == 401:
                hint = ("（认证失败：Key 无效，或 Key 与地域不匹配 —— "
                        "这些模型要【北京地域】的 Key）")
            elif response.status_code == 400:
                hint = ("（请求被拒：这段文本合不出来（含念不出的字符？），"
                        "或并发/QPS 超限、账号额度不足 —— 详情见服务端日志）")
            err = RuntimeError(f"HTTP {response.status_code} @ {endpoint} {hint} {detail}".strip())
            # 400 是"输入问题"，不该让整台设备的发声熔断 120 秒
            if response.status_code == 400:
                note_input_error(str(err))
                _LAST_MS = (time.time() - t0) * 1000
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
            raise err

        body = response.json()
        # 注意：**不能**要求 body["status_code"] == 200 —— 实测音色合成的成功响应里
        # 根本没有这个字段（文档示例里有，真实返回里没有）。早期按文档写成硬性校验，
        # 导致所有合法响应都被判成失败。这里只在字段确实存在且非 200 时才算错。
        code = body.get("status_code")
        err_code = body.get("code")
        if (code is not None and int(code) != 200) or err_code:
            raise RuntimeError(f"{err_code or code}: {str(body.get('message') or '')[:160]}")

        audio = ((body.get("output") or {}).get("audio")) or {}
        raw = b""
        # 非流式的正常路径：data 为空、url 有值 → 再下载一次
        import base64
        data_b64 = str(audio.get("data") or "").strip()
        if data_b64:
            raw = normalize_wav_header(base64.b64decode(data_b64))
        elif audio.get("url"):
            raw = _download(str(audio["url"]), limit)
        if not raw:
            raise RuntimeError("返回音频为空（data 与 url 都为空）")

        # 语速：本地时间轴重采样（接口本身不带语速参数）
        raw = _apply_speed(raw, _speed_from_rate(rate))
        out_path.write_bytes(raw)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:120]}"
        note_failure(msg)
        _LAST_MS = (time.time() - t0) * 1000
        _LAST_ERROR = msg
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    if not out_path.exists() or out_path.stat().st_size < 512:
        note_failure("音频文件过小")
        _LAST_MS = (time.time() - t0) * 1000
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    note_success()
    _LAST_MS = (time.time() - t0) * 1000
    return True
