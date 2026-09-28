"""语音特征分析 —— 语速（字/分钟）等，供风险预警（焦虑/低落倾向）使用。"""
from __future__ import annotations

import json
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .asr import transcribe_wav


def analyze_voice(wav_path: Path, store=None, model_path: Path | None = None) -> Dict[str, Any]:
    """识别 WAV → 语速特征 → 落库 wakeword 事件（含 speech_word_per_min）。

    语速 = 识别字符数（去空白） / 语音时长(分钟)。
    """
    text, conf = "", 0.0
    error = None
    try:
        text, conf = transcribe_wav(wav_path, model_path)
    except Exception as exc:
        error = str(exc)

    duration_sec = 0.0
    try:
        with wave.open(str(wav_path), "rb") as wav:
            duration_sec = wav.getnframes() / max(wav.getframerate(), 1)
    except Exception:
        pass

    chars = len([c for c in text if not c.isspace()])
    speech_rate = round(chars / (duration_sec / 60), 1) if duration_sec > 0.5 else 0.0

    result: Dict[str, Any] = {
        "text": text,
        "confidence": round(conf, 3),
        "duration_sec": round(duration_sec, 2),
        "chars": chars,
        "speech_rate_chars_per_min": speech_rate,
        "speech_word_per_min": speech_rate,   # analytics 兼容字段
        "record_ts": datetime.now(timezone.utc).timestamp(),
    }
    if error:
        result["error"] = error
    if store is not None and text:
        store.add_event("wakeword", result, status="local")
    return result


def record_speech_rate(text: str, duration_sec: float, store=None,
                       user_id: str = "default") -> Dict[str, Any]:
    """用「已识别文本 + 音频时长」算语速并落库。

    设备页每次说话都会走 /api/asr，那里已经有识别结果，再调用 analyze_voice
    会二次识别（慢一倍）；所以直接复用文本算语速。分析的"平均语速"读的就是
    这里的 wakeword 事件。
    """
    chars = len([c for c in (text or "") if not c.isspace()])
    rate = round(chars / (duration_sec / 60), 1) if duration_sec > 0.5 else 0.0
    result: Dict[str, Any] = {
        "user_id": user_id,
        "text": text,
        "duration_sec": round(duration_sec, 2),
        "chars": chars,
        "speech_rate_chars_per_min": rate,
        "speech_word_per_min": rate,      # analytics 兼容字段
        "record_ts": datetime.now(timezone.utc).timestamp(),
        "source": "asr",
    }
    if store is not None and chars:
        store.add_event("wakeword", result, status="local")
    return result
