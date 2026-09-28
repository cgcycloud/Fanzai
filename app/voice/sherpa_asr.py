"""sherpa-onnx Paraformer 中文识别引擎（默认引擎）。

为什么用它而不是 Vosk-small：
  - 实测同一台机器（含摄像头满载），15.9 秒语音解码 0.21 秒 vs Vosk 3.42 秒
  - 准确率明显更高（Vosk-small 把"粥"听成"中"，Paraformer 正确）
  - int8 模型 82MB，树莓派可跑；sherpa-onnx 提供 aarch64 轮子

依赖为可选项：没装 sherpa-onnx 或缺模型时 available=False，调用方据此提示下载模型。
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from .. import config

_LOCK = threading.Lock()
_RECOGNIZER = None
_LOAD_ERROR: str | None = None


def model_dir() -> Path:
    return config.PATHS.models_dir / config.SHERPA_MODEL_DIRNAME


def available() -> bool:
    """模型文件是否就位（不触发加载）。"""
    d = model_dir()
    return (d / "model.int8.onnx").exists() and (d / "tokens.txt").exists()


def _build():
    """加载识别器（单例，加锁避免并发重复加载 82MB 模型）。"""
    global _RECOGNIZER, _LOAD_ERROR
    if _RECOGNIZER is not None:
        return _RECOGNIZER
    with _LOCK:
        if _RECOGNIZER is not None:
            return _RECOGNIZER
        import sherpa_onnx
        d = model_dir()
        _RECOGNIZER = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=config.SHERPA_NUM_THREADS,
            sample_rate=config.ASR_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
        )
        return _RECOGNIZER


def warmup() -> bool:
    """预热：提前把 82MB 模型载入内存（首次识别可省 1~2 秒）。"""
    global _LOAD_ERROR
    try:
        if not available():
            _LOAD_ERROR = f"模型缺失: {model_dir()}"
            return False
        _build()
        _LOAD_ERROR = None
        return True
    except Exception as exc:
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def load_error() -> str | None:
    return _LOAD_ERROR


def _read_mono16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("Paraformer 需要 16k 单声道 16bit WAV")
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if rate != config.ASR_SAMPLE_RATE:
        ratio = rate / config.ASR_SAMPLE_RATE
        n = int(data.size / ratio)
        idx = np.arange(n, dtype=np.float64) * ratio
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, data.size - 1)
        frac = (idx - i0).astype(np.float32)
        data = (data[i0] * (1 - frac) + data[i1] * frac).astype(np.float32)
    return data


def transcribe_wav(wav_path: Path) -> str:
    """识别 16k 单声道 WAV → 文本（Paraformer 输出不带词间空格）。"""
    rec = _build()
    samples = _read_mono16k(Path(wav_path))
    if samples.size == 0:
        return ""
    stream = rec.create_stream()
    stream.accept_waveform(config.ASR_SAMPLE_RATE, samples)
    with _LOCK:                      # 单例识别器非线程安全，串行解码
        rec.decode_stream(stream)
    return str(stream.result.text or "").strip()
