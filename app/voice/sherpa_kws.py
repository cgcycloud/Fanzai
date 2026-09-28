"""sherpa-onnx KWS 中文关键词唤醒（zipformer，建模单元=拼音声母+韵母）。

为什么换掉 Vosk 方案（原 app/voice/wake.py）：
  * vosk-model-small-cn 词表里**没有「崽」**：语法限制会退化成 `你好 [unk]`（永远无法命中），
    自由识别又把「饭崽」听成「贩灾」，只能靠拼音容错兜 —— 噪声里还容易同音误命中。
  * 本模型专门为中文唤醒词训练（wenetspeech 1 万小时，3.3M 参数，int8 仅 4.7MB），
    token 就是拼音，自定义唤醒词只需一行 `n ǐ h ǎo f àn z ǎi @你好饭崽`，
    流式解码、延迟低、阈值可调，且**唤醒词可以放生僻字**。

接口与 app/voice/wake.py 的 WakeDetector 保持一致（feed 返回同形 dict），
模型缺失时 feed() 返回 reason="no-model"，由上层提示下载（不再回退到已删除的 Vosk）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import config

# 声母表：按长度从长到短匹配，否则 "zh" 会被误拆成 "z"+"h"
_INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
             "j", "q", "x", "r", "z", "c", "s", "y", "w")


def model_dir() -> Path:
    return config.PATHS.models_dir / config.SHERPA_KWS_DIRNAME


def _model_files() -> dict[str, Path]:
    d = model_dir()
    ep = config.SHERPA_KWS_EPOCH
    return {
        "encoder": d / f"encoder-{ep}-chunk-16-left-64.int8.onnx",
        "decoder": d / f"decoder-{ep}-chunk-16-left-64.int8.onnx",
        "joiner": d / f"joiner-{ep}-chunk-16-left-64.int8.onnx",
        "tokens": d / "tokens.txt",
    }


def available() -> bool:
    """模型文件是否就位（不触发加载）。"""
    files = _model_files()
    if not all(p.exists() for p in files.values()):
        # int8 缺失时允许退回非量化权重
        ep = config.SHERPA_KWS_EPOCH
        d = model_dir()
        alt = {
            "encoder": d / f"encoder-{ep}-chunk-16-left-64.onnx",
            "decoder": d / f"decoder-{ep}-chunk-16-left-64.onnx",
            "joiner": d / f"joiner-{ep}-chunk-16-left-64.onnx",
        }
        return (files["tokens"].exists()
                and all(p.exists() for p in alt.values()))
    return True


def _resolved_files() -> dict[str, Path]:
    files = _model_files()
    if all(p.exists() for p in files.values()):
        return files
    ep = config.SHERPA_KWS_EPOCH
    d = model_dir()
    return {
        "encoder": d / f"encoder-{ep}-chunk-16-left-64.onnx",
        "decoder": d / f"decoder-{ep}-chunk-16-left-64.onnx",
        "joiner": d / f"joiner-{ep}-chunk-16-left-64.onnx",
        "tokens": files["tokens"],
    }


_load_lock = threading.Lock()
_tokens_cache: set[str] | None = None


def _tokens() -> set[str]:
    global _tokens_cache
    if _tokens_cache is None:
        path = _model_files()["tokens"]
        out: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.rsplit(" ", 1)
                if parts and parts[0].strip():
                    out.add(parts[0].strip())
        except Exception:
            pass
        _tokens_cache = out
    return _tokens_cache


def _split_syllable(syllable: str) -> list[str]:
    """拼音音节 → 声母 + 韵母（模型 token 就是这两部分）；无声母则只返回韵母。"""
    for ini in _INITIALS:
        if syllable.startswith(ini) and len(syllable) > len(ini):
            return [ini, syllable[len(ini):]]
    return [syllable]


def keyword_line(text: str) -> tuple[str, list[str]]:
    """中文唤醒词 → KWS 关键词行；返回 (关键词行, 模型词表里缺失的 token)。

    注意：带声调的拼音（ǐ ǎo àn）本身就是非 ASCII，不能拿 isascii() 过滤，
    只能用模型 tokens 表校验。
    """
    clean = str(text or "").strip()
    if not clean:
        return "", []
    try:
        from pypinyin import Style, lazy_pinyin
    except Exception:
        return "", ["<缺少 pypinyin>"]
    try:
        syllables = lazy_pinyin(clean, style=Style.TONE, errors="ignore")
    except Exception:
        return "", ["<pypinyin 转换失败>"]
    tokens: list[str] = []
    for syl in syllables:
        syl = str(syl).strip()
        if not syl or not any(ch.isalpha() for ch in syl):
            continue
        tokens.extend(_split_syllable(syl))
    if not tokens:
        return "", ["<无法转拼音>"]
    known = _tokens()
    missing = [t for t in tokens if known and t not in known]
    return " ".join(tokens) + " @" + clean, missing


def keywords_text(words: list[str]) -> tuple[str, list[str]]:
    """多个唤醒词 → 多行关键词文本；返回 (文本, 被跳过的词)。"""
    lines: list[str] = []
    skipped: list[str] = []
    for word in words or []:
        line, missing = keyword_line(word)
        if line and not missing:
            lines.append(line)
        else:
            skipped.append(str(word))
    return "\n".join(lines) + ("\n" if lines else ""), skipped


class KwsDetector:
    """会话式关键词唤醒检测器（进程内单例：kws_detector）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spotter = None
        self._load_error: str | None = None
        self._sessions: dict[str, dict] = {}
        self._hits = 0
        self._last_hit_at = 0.0
        self._last_word = ""
        self._last_level = 0.0
        self._last_level_at = 0.0

    # ---------- 模型加载 ----------
    def available(self) -> bool:
        """模型文件是否就位（供启动预热与状态展示调用）。"""
        return available()

    def load_error(self) -> str | None:
        return self._load_error

    def ready(self) -> bool:
        if self._spotter is not None:
            return True
        try:
            return self._build() is not None
        except Exception:
            return False

    def _build(self):
        if self._spotter is not None:
            return self._spotter
        if not available():
            self._load_error = f"KWS 模型缺失: {model_dir()}"
            return None
        with _load_lock:
            if self._spotter is not None:
                return self._spotter
            try:
                import sherpa_onnx
            except Exception as exc:
                self._load_error = f"sherpa-onnx 不可用: {exc}"
                return None
            files = _resolved_files()
            # KeywordSpotter 构造要求一个关键词文件；真正的唤醒词按会话传入，
            # 这里放一份默认词，便于模型自检与无参调用。
            default_kw = config.PATHS.data_dir / "wake_keywords.txt"
            try:
                default_kw.parent.mkdir(parents=True, exist_ok=True)
                if not default_kw.exists() or default_kw.stat().st_size == 0:
                    text, _ = keywords_text(["你好饭崽"])
                    default_kw.write_text(text or "n ǐ h ǎo f àn z ǎi @你好饭崽\n",
                                          encoding="utf-8")
            except Exception:
                pass
            try:
                self._spotter = sherpa_onnx.KeywordSpotter(
                    tokens=str(files["tokens"]),
                    encoder=str(files["encoder"]),
                    decoder=str(files["decoder"]),
                    joiner=str(files["joiner"]),
                    keywords_file=str(default_kw),
                    num_threads=max(1, int(getattr(config, "KWS_NUM_THREADS", 1))),
                    sample_rate=config.ASR_SAMPLE_RATE,
                    keywords_score=float(config.KWS_SCORE),
                    keywords_threshold=float(config.KWS_THRESHOLD),
                )
                self._load_error = None
            except Exception as exc:
                self._load_error = f"{type(exc).__name__}: {exc}"
                self._spotter = None
        return self._spotter

    def warmup(self) -> bool:
        return self._build() is not None

    # ---------- 会话 ----------
    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def drop_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _reset_idle_stream(self, session: dict, now: float, quiet: bool) -> None:
        """定期把唤醒流归零（**这是长时间运行的性能关键**）。

        sherpa 的 OnlineStream 是**只进不退**的：浏览器每 450ms 喂一片，
        如果一直没命中唤醒词就永远不 reset，解码器状态会一直涨 ——
        实测连跑 3 小时后同一个进程从 0.2 核涨到 3.2 核、唤醒越来越慢。
        所以：安静时每 KWS_STREAM_RESET_SEC 归零一次（不会切在说话中间），
        环境一直吵时也按 KWS_STREAM_MAX_SEC 兜底归零。
        """
        started = float(session.get("stream_at") or 0.0)
        if not started:
            session["stream_at"] = now
            return
        age = now - started
        if not quiet and age < float(config.KWS_STREAM_MAX_SEC):
            return
        if age < float(config.KWS_STREAM_RESET_SEC):
            return
        try:
            self._spotter.reset_stream(session["stream"])
        except Exception:
            session["stream"] = None
        session["stream_at"] = now
        session["resets"] = int(session.get("resets") or 0) + 1

    def _drop_stale_sessions(self, now: float) -> None:
        """浏览器页面被强杀（没走 wake/reset）时留下的会话要能自己消失。"""
        ttl = float(config.KWS_SESSION_TTL_SEC)
        for sid in [k for k, v in self._sessions.items()
                    if now - float(v.get("last_feed") or 0.0) > ttl]:
            self._sessions.pop(sid, None)

    def status(self) -> dict:
        with self._lock:
            return {
                "engine": "sherpa-kws",
                "model": config.SHERPA_KWS_DIRNAME,
                "available": available(),
                "loaded": self._spotter is not None,
                "error": self._load_error,
                "sessions": len(self._sessions),
                "resets": sum(int(v.get("resets") or 0) for v in self._sessions.values()),
                "hits": self._hits,
                "last_hit_at": round(self._last_hit_at, 1),
                "last_word": self._last_word,
                "last_level": round(self._last_level, 4),
                "last_level_at": round(self._last_level_at, 1),
                "threshold": float(config.KWS_THRESHOLD),
            }

    # ---------- 检测 ----------
    def feed(self, session_id: str, pcm: bytes, *, words: list[str],
             cooldown_sec: float = 2.0, max_hits_per_min: int = 5,
             now: float | None = None, **_ignored) -> dict:
        """喂入 16k 单声道 S16_LE PCM，返回 {matched, word, text, confidence}。

        接口与 voice/wake.py 的 WakeDetector 对齐，便于 API 层按引擎可用性切换。
        """
        now = time.time() if now is None else now
        if not pcm:
            return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                    "reason": "empty"}
        spotter = self._build()
        if spotter is None:
            return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                    "reason": "no-model"}

        kw_text, skipped = keywords_text(words)
        wanted = [w for w in (words or []) if w and w not in skipped]
        if not kw_text.strip() or not wanted:
            return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                    "reason": "no-keywords"}

        try:
            import numpy as np
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            return {"matched": False, "word": "", "text": "", "confidence": 0.0}

        signature = tuple(wanted)
        with self._lock:
            self._last_level = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            self._last_level_at = now
            quiet = self._last_level < float(config.KWS_QUIET_RMS)
            self._drop_stale_sessions(now)
            session = self._sessions.get(session_id)
            if session is None or session["words"] != signature:
                try:
                    stream = spotter.create_stream(keywords=kw_text)
                except Exception as exc:
                    return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                            "reason": f"create-stream:{exc}"}
                session = {"stream": stream, "words": signature,
                           "last_hit": 0.0, "hit_times": [], "stream_at": now, "resets": 0}
                self._sessions[session_id] = session
            stream = session["stream"]
            session["last_feed"] = now
            # 长时间静默时把解码流归零：不做的话解码器状态无限增长（CPU 越跑越高）
            self._reset_idle_stream(session, now, quiet)
            if session.get("stream") is None:              # 归零失败 → 重建一个
                try:
                    session["stream"] = spotter.create_stream(keywords=kw_text)
                    session["stream_at"] = now
                except Exception:
                    return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                            "reason": "recreate-stream"}
            stream = session["stream"]
            try:
                stream.accept_waveform(config.ASR_SAMPLE_RATE, samples)
                raw = ""
                # 流式：每次喂入后把所有就绪的分片解出来
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                    raw = spotter.get_result(stream) or raw
            except Exception as exc:
                return {"matched": False, "word": "", "text": "", "confidence": 0.0,
                        "reason": f"decode:{exc}"}

            word = self._clean_result(raw)
            if not word:
                return {"matched": False, "word": "", "text": "", "confidence": 0.0}

            # 冷却：同一句话不要连续触发（浏览器端也有去抖，这里服务端兜底）
            if (now - session["last_hit"]) < cooldown_sec:
                return {"matched": False, "word": word, "text": word, "confidence": 1.0,
                        "reason": "cooldown"}
            # 限流：嘈杂环境里兜住"被反复刷醒"
            session["hit_times"] = [t for t in session["hit_times"] if now - t <= 60.0]
            if len(session["hit_times"]) >= max(1, int(max_hits_per_min)):
                return {"matched": False, "word": word, "text": word, "confidence": 1.0,
                        "reason": "rate-limited"}

            session["last_hit"] = now
            session["hit_times"].append(now)
            try:
                spotter.reset_stream(stream)     # 命中后重置，继续监听下一句
            except Exception:
                self._sessions.pop(session_id, None)
            session["stream_at"] = now
            self._hits += 1
            self._last_hit_at = now
            self._last_word = word
            return {"matched": True, "word": word, "text": word, "confidence": 1.0}

    @staticmethod
    def _clean_result(raw) -> str:
        """get_result 返回关键词或 JSON 串，统一取到可展示的词。"""
        if not raw:
            return ""
        text = str(raw).strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:
                return ""
            text = str(data.get("keyword") or data.get("text") or "").strip()
        if "@" in text:                      # `n ǐ h ǎo @你好` → 取 @ 后面的中文
            text = text.split("@", 1)[1].strip()
        return text


kws_detector = KwsDetector()
