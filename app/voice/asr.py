"""语音识别（ASR）—— sherpa-onnx Paraformer 中文模型。

为什么只用 Paraformer：
  * 实测比 Vosk-small 快 16~31 倍（15.9 秒语音 0.26 秒 vs 3.42 秒），准确率明显更高
    （Vosk-small 会把"粥"听成"中"）。
  * 旧的 Vosk 引擎/唤醒监听已全部删除：识别用 Paraformer，唤醒用 KWS，
    整套都基于 sherpa-onnx。发声已改为云端 Qwen3-TTS（见 qwen_tts_engine.py），
    不再使用任何本地 TTS 模型。

本模块保留 `transcribe_wav` / `warmup` / `active_engine` 这层薄封装，
方便 API 层与测试引用；模型缺失时给出可执行的提示，而不是静默降级到已删除的引擎。
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from .. import config
from . import sherpa_asr


class ModelMissing(RuntimeError):
    """识别模型缺失（提示用户去下载，而不是回退到已删除的引擎）。"""


def active_engine() -> str:
    return "paraformer" if sherpa_asr.available() else "unavailable"


def model_dir() -> Path:
    return sherpa_asr.model_dir()


def load_error() -> str | None:
    return sherpa_asr.load_error()


def warmup(model_path: Path | None = None) -> bool:
    """预热识别模型（首次识别可省 1~2 秒加载时间）。"""
    return sherpa_asr.warmup()


def transcribe_wav(wav_path: Path, model_path: Path | None = None) -> Tuple[str, float]:
    """识别 16k 单声道 WAV → (文本, 置信度)。

    Paraformer 不提供词级置信度，统一返回 1.0（置信度仅旧的唤醒词门槛用过，
    现在唤醒由 KWS 负责，不再需要）。
    """
    if not sherpa_asr.available():
        raise ModelMissing(
            f"识别模型缺失: {sherpa_asr.model_dir()}；"
            "模型随发行包附带（models/sherpa-onnx-paraformer-zh-small/），"
            "若被删掉请重新解压发行包或按 README「模型」一节补回")
    text = sherpa_asr.transcribe_wav(Path(wav_path))
    return str(text or "").strip(), 1.0


def sample_rate() -> int:
    return int(config.ASR_SAMPLE_RATE)
