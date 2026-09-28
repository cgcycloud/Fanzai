"""TTS 语音合成 —— 统一入口：**Qwen3-TTS-Flash（阿里云百炼）**。

链路（现在只有这一条云端链路 + 一个本地兜底）：

    Qwen3-TTS-Flash（云端，唯一发声引擎）→ 本地 880Hz 提示音（合成失败时的兜底）

* 引擎实现在 `qwen_tts_engine.py`：DashScope `multimodal-generation` 接口，
  非流式返回音频 URL 再下载，带连续失败熔断。
* 接口参数（端点 / 模型 / 音色 / 语种 / Key）全部走 `data_local/ai_config.json`，
  设置页可改 —— `qwen_tts_engine._settings()` 每次读配置，改完立即生效，不用重启。
* **本地 TTS 模型（Kokoro / Matcha / VITS / Piper / edge-tts）已全部移除**，
  所以这里不再有"离线音色"这一层；网络不通时由上层降级成提示音。

输出容器不固定：正常是接口返回的 WAV，兜底是本地生成的提示音 WAV。前端仍应按
**实际字节**选 MIME（`detect_format()` → `mime_of()`），历史上这里搞错过一次，
表现是"有字没声"。
"""
from __future__ import annotations

from pathlib import Path

from . import qwen_tts_engine

# 引擎标识：配置/状态接口里用它表示"当前引擎"
ENGINE_QWEN = "qwen3-tts"


def detect_format(data: bytes | None) -> str:
    """按文件头判断音频容器：'wav' / 'mp3'（识别不了按 mp3）。"""
    if not data:
        return "mp3"
    head = bytes(data[:4])
    if head[:4] == b"RIFF":
        return "wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    return "mp3"


def mime_of(fmt: str) -> str:
    return {"wav": "audio/wav", "mp3": "audio/mpeg"}.get(str(fmt or "").lower(), "audio/mpeg")


def available_engines() -> list[str]:
    """当前能出声的引擎。只有一个：配了 Key 且不在熔断期时才列出来。"""
    return [ENGINE_QWEN] if qwen_tts_engine.available() else []


def active_engine() -> str:
    """实际会用的引擎。

    "unavailable" 表示接口当前不可用（没配 Key 或熔断中）——上层据此直接走提示音，
    绝不返回 None 让上层炸。
    """
    return ENGINE_QWEN if qwen_tts_engine.available() else "unavailable"


def engine_options() -> list[dict]:
    """设置面板可选项。只剩一个引擎，这里只用于展示当前状态（不可切换）。"""
    return [{
        "key": ENGINE_QWEN,
        "name": "Qwen3-TTS-Flash",
        "desc": f"阿里云百炼 qwen3-tts-flash（{qwen_tts_engine.default_voice()}）"
                f"｜需联网，非离线",
        "sample_rate": 24000,
        "available": qwen_tts_engine.available(),
        "offline": False,
        "commercial": "yes",
        "license_note": "阿里云百炼商用服务，按字符计费",
    }]


def speaker_options() -> list[dict]:
    """多说话人候选：本引擎音色由接口参数决定，不在面板里切换 → 空。"""
    return []


def speaker_count(key: str | None = None) -> int:
    return 1


def cache_voice(client=None) -> str:
    """TTS 缓存键里的"音色"部分。

    必须把模型与音色都带进去：换了模型/音色后若还沿用旧的键，
    缓存会命中旧音色的音频（听起来没变）。
    语速由 tts_cache 单独拼进键。
    """
    return f"{ENGINE_QWEN}:{qwen_tts_engine.model_name()}:{qwen_tts_engine.default_voice()}"


def synthesize(text: str, output_path: Path, voice: str | None = None,
               rate: str | None = None, engine: str | None = None,
               sid: int | None = None) -> bool:
    """合成语音到文件；成功返回 True。

    voice / rate 只用于临时覆盖（调试或未来接其它音色），不改变用户设置。
    合成失败即返回 False，由调用方决定是否降级成提示音
    （`core.ai_client.write_beep_tone`）。
    """
    text = str(text or "").strip()
    if not text:
        return False
    if not qwen_tts_engine.available():
        return False
    return qwen_tts_engine.synthesize(text, Path(output_path), voice=voice, rate=rate)


def status() -> dict:
    """给状态接口/自检用。"""
    return {
        "active": active_engine(),
        "available": available_engines(),
        "qwen": qwen_tts_engine.status(),
    }
