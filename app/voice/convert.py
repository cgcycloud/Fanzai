"""音频转码 —— 任意输入 → 16kHz 单声道 16bit WAV（识别引擎要求）。

为什么不用 pydub：Python 3.13 起标准库移除了 `audioop`（PEP 594），
pydub 依赖它，在新版 Python（含树莓派新系统）上直接 ImportError。

策略（按优先级）：
  1. 已是 WAV（RIFF/WAVE）→ 用标准库 wave + numpy 自行混音/重采样，零外部依赖
  2. 压缩格式（webm/opus/mp3/m4a）→ 调用 ffmpeg 管道（若系统有）
  3. 都不行 → 抛出明确错误，提示前端转码或安装 ffmpeg
"""
from __future__ import annotations

import io
import shutil
import subprocess
import wave

import numpy as np

TARGET_RATE = 16000


def _decode_wav(raw: bytes) -> tuple[np.ndarray, int, int]:
    """WAV → (float32 单声道样本, 原采样率, 原声道数)"""
    with wave.open(io.BytesIO(raw), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if width == 1:      # 8bit 无符号
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:    # 16bit
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:    # 24bit
        b = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v & 0x800000, v - 0x1000000, v)
        data = v.astype(np.float32) / 8388608.0
    elif width == 4:    # 32bit int
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"不支持的采样位宽: {width * 8}bit")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate, channels


def _resample(data: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """线性插值重采样（语音识别足够，无需多相滤波）。"""
    if src_rate == dst_rate or data.size == 0:
        return data
    ratio = src_rate / dst_rate
    out_len = int(data.size / ratio)
    idx = np.arange(out_len, dtype=np.float64) * ratio
    i0 = np.floor(idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, data.size - 1)
    frac = (idx - i0).astype(np.float32)
    return (data[i0] * (1 - frac) + data[i1] * frac).astype(np.float32)


def _encode_wav(data: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _ffmpeg_convert(raw: bytes) -> bytes:
    """用 ffmpeg 管道解码压缩格式（webm/opus/mp3/m4a → 16k mono wav）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ar", str(TARGET_RATE), "-ac", "1", "-f", "wav", "pipe:1"],
        input=raw, capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or len(proc.stdout) < 44:
        raise RuntimeError(
            "音频解码失败（ffmpeg）：" + proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def is_wav(raw: bytes) -> bool:
    return len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def to_16k_wav(raw: bytes) -> bytes:
    """任意音频字节 → 16kHz 单声道 16bit WAV 字节（供识别引擎直接使用）。"""
    if not raw:
        raise RuntimeError("音频为空")
    if is_wav(raw):
        data, rate, _ = _decode_wav(raw)
        data = _resample(data, rate)
        if data.size < TARGET_RATE // 5:      # 少于 0.2 秒，判定为无效录音
            raise RuntimeError("录音过短（不足 0.2 秒），请按住说完一句话")
        return _encode_wav(data)
    if shutil.which("ffmpeg"):
        wav_bytes = _ffmpeg_convert(raw)
        data, rate, _ = _decode_wav(wav_bytes)
        return _encode_wav(_resample(data, rate))
    raise RuntimeError(
        "收到的是压缩音频（如 webm/opus），但本机没有 ffmpeg 无法转码。"
        "请在浏览器端转成 WAV，或安装 ffmpeg（树莓派: sudo apt install ffmpeg）")
