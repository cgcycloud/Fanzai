"""AI 配置存储 —— API Key 仅以 Fernet 加密落盘，绝不明文。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import PATHS, DEFAULT_SYSTEM_PROMPT


def _is_legacy_default_prompt(prompt: str) -> bool:
    """判断存量提示词是否只是"旧版默认预设"（用户自己改过就不动）。

    旧默认里写死了“场景应对原则”和固定话术，AI 会照着复读；这里识别出来
    并在读取时升级为新的预设，避免老配置把回复一直压成模板腔。
    """
    return "场景应对原则" in prompt and "正念饭崽" in prompt


# 旧版发声引擎的模型名 / 音色名。发行版 TTS 换成了 Qwen3-TTS-Flash，
# 这些值留在配置里会让新引擎把旧名字当参数发出去（例如 model=glm-4-voice），
# 请求必然失败 —— 读取时识别并升级成本次默认值。
_LEGACY_TTS_MODELS = {"glm-4-voice", ""}
_LEGACY_TTS_VOICES = {"zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
                      "zh-CN-YunyangNeural", "zh-CN-YunjianNeural"}


def _clean_tts_model(value) -> str:
    model = str(value or "").strip()
    return "qwen3-tts-flash" if model in _LEGACY_TTS_MODELS else model


def _clean_tts_voice(value) -> str:
    voice = str(value or "").strip()
    return "Cherry" if voice in _LEGACY_TTS_VOICES else (voice or "Cherry")


@dataclass(frozen=True)
class AIConfig:
    api_url: str
    model: str
    # api_key / tts_api_key 是**明文**，直接写在 data_local/ai_config.json 里，
    # 方便你用手改文件（这是唯一那份 AI 配置，没有配置界面）。
    # 旧的 encrypted_* 字段仍能读，见 load()。
    api_key: str = ""
    encrypted_api_key: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Qwen3 系模型默认先"思考"再回答：实测首字 16.7s，关掉后 0.9s
    # （语音助手必须关，见 ai_client._thinking_params）。只对 qwen3 系模型生效。
    enable_thinking: bool = False
    # ---- 发声（TTS）：Qwen3-TTS-Flash / 阿里云百炼 ----
    # tts_api_url 留空即用 config.QWEN_TTS_API_BASE（北京地域端点）；
    # 换新加坡地域时这里填对应的 base URL，并配一把该地域的 Key。
    tts_api_url: str = ""
    tts_api_key: str = ""
    encrypted_tts_api_key: str | None = None
    tts_model: str = "qwen3-tts-flash"
    tts_voice: str = "Cherry"
    tts_language: str = "Chinese"
    tts_format: str = "wav"
    # ---- 视觉（摄像头画面分析）----
    # vision_api_url / vision_api_key 留空就**沿用对话那组**（单厂商时不用填）。
    # 对话与视觉不是同一个厂商时（例如对话走百炼 Qwen、视觉仍用智谱 GLM-4V）必须填，
    # 否则视觉请求会被发到对话厂商的端点上（模型不存在 → 每 10 秒一次 400）。
    vision_model: str = "glm-4v-flash"
    vision_api_url: str = ""
    vision_api_key: str = ""

    @property
    def key_set(self) -> bool:
        return bool(self.api_key)

    @property
    def tts_key_set(self) -> bool:
        return bool(self.tts_api_key)

    @property
    def vision_connection(self) -> tuple[str, str]:
        """视觉用哪组 (api_url, api_key)：单独配了就用它，否则沿用对话那组。"""
        return (self.vision_api_url or self.api_url,
                self.vision_api_key or self.api_key)


class AIConfigStore:
    def __init__(self, config_path: Path | None = None, key_path: Path | None = None):
        self.config_path = config_path or PATHS.ai_config_path
        self.key_path = key_path or PATHS.ai_key_path

    def _fernet(self) -> Fernet:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return Fernet(self.key_path.read_bytes())

    def encrypt_key(self, api_key: str) -> str:
        return self._fernet().encrypt(api_key.encode("utf-8")).decode("ascii")

    def decrypt_key(self, encrypted_api_key: str | None) -> str | None:
        if not encrypted_api_key:
            return None
        try:
            return self._fernet().decrypt(encrypted_api_key.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("本机密钥无法解密已存的 API Key（key 文件可能被更换）") from exc

    def _resolve_key(self, data: dict, plain_field: str, enc_field: str) -> str:
        """取出一把可用的 Key：**明文优先**，其次解密旧的 encrypted_* 字段。

        解密失败不抛异常：这是个可手改的文件，写错一个字段不该让整个服务起不来，
        拿不到 Key 就当作"未配置"，上层会明确提示。
        """
        plain = str(data.get(plain_field) or "").strip()
        if plain:
            return plain
        enc = data.get(enc_field)
        if not enc:
            return ""
        try:
            return (self.decrypt_key(enc) or "").strip()
        except Exception:
            return ""

    def load(self) -> AIConfig | None:
        if not self.config_path.exists():
            return None
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"读取 {self.config_path} 失败（JSON 格式错误？）：{exc}") from exc
        prompt = data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        if _is_legacy_default_prompt(prompt):
            prompt = DEFAULT_SYSTEM_PROMPT
        return AIConfig(
            api_url=data.get("api_url", "").rstrip("/"),
            model=data.get("model", ""),
            api_key=self._resolve_key(data, "api_key", "encrypted_api_key"),
            encrypted_api_key=data.get("encrypted_api_key"),
            system_prompt=prompt,
            enable_thinking=bool(data.get("enable_thinking", False)),
            tts_api_url=data.get("tts_api_url", "").rstrip("/"),
            tts_api_key=self._resolve_key(data, "tts_api_key", "encrypted_tts_api_key"),
            encrypted_tts_api_key=data.get("encrypted_tts_api_key"),
            tts_model=_clean_tts_model(data.get("tts_model")),
            tts_voice=_clean_tts_voice(data.get("tts_voice")),
            tts_language=data.get("tts_language") or "Chinese",
            tts_format=data.get("tts_format") or "wav",
            vision_model=data.get("vision_model") or "glm-4v-flash",
            vision_api_url=str(data.get("vision_api_url") or "").rstrip("/"),
            vision_api_key=str(data.get("vision_api_key") or "").strip(),
        )

    def save(
        self,
        api_url: str,
        model: str,
        api_key: str | None = None,
        system_prompt: str | None = None,
        enable_thinking: bool | None = None,
        tts_api_url: str | None = None,
        tts_api_key: str | None = None,
        tts_model: str | None = None,
        tts_voice: str | None = None,
        tts_language: str | None = None,
        tts_format: str | None = None,
        vision_model: str | None = None,
        vision_api_url: str | None = None,
        vision_api_key: str | None = None,
    ) -> AIConfig:
        """写回配置。Key 以**明文**保存，保持文件可手改。

        旧的 encrypted_* 字段不再写入 —— 一旦保存过，文件里只剩明文 Key 与
        其它可读字段；`ai_config.key`（加密用的本机密钥）也随之不再需要。
        """
        current = self.load()
        resolved_key = str(api_key or (current.api_key if current else "") or "").strip()
        resolved_tts_key = str(tts_api_key or (current.tts_api_key if current else "") or "").strip()
        config = AIConfig(
            api_url=api_url.rstrip("/"),
            model=model,
            api_key=resolved_key,
            system_prompt=system_prompt or (current.system_prompt if current else DEFAULT_SYSTEM_PROMPT),
            enable_thinking=(bool(enable_thinking) if enable_thinking is not None
                             else (current.enable_thinking if current else False)),
            tts_api_url=(tts_api_url.rstrip("/") if tts_api_url is not None else (current.tts_api_url if current else "")),
            tts_api_key=resolved_tts_key,
            tts_model=_clean_tts_model(tts_model) if tts_model else (current.tts_model if current else "qwen3-tts-flash"),
            tts_voice=_clean_tts_voice(tts_voice) if tts_voice else (current.tts_voice if current else "Cherry"),
            tts_language=tts_language or (current.tts_language if current else "Chinese"),
            tts_format=tts_format or (current.tts_format if current else "wav"),
            vision_model=vision_model or (current.vision_model if current else "glm-4v-flash"),
            vision_api_url=(vision_api_url.rstrip("/") if vision_api_url is not None
                            else (current.vision_api_url if current else "")),
            vision_api_key=(str(vision_api_key).strip() if vision_api_key is not None
                            else (current.vision_api_key if current else "")),
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "api_url": config.api_url,
                    "model": config.model,
                    "api_key": config.api_key,
                    "system_prompt": config.system_prompt,
                    "enable_thinking": config.enable_thinking,
                    "vision_model": config.vision_model,
                    "vision_api_url": config.vision_api_url,
                    "vision_api_key": config.vision_api_key,
                    "tts_api_url": config.tts_api_url,
                    "tts_api_key": config.tts_api_key,
                    "tts_model": config.tts_model,
                    "tts_voice": config.tts_voice,
                    "tts_language": config.tts_language,
                    "tts_format": config.tts_format,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass
        return config

    def public_dict(self) -> dict[str, object]:
        """给前端的配置视图：绝不含密钥本体。"""
        config = self.load()
        if not config:
            return {
                "configured": False, "api_url": "", "model": "", "key_set": False,
                "system_prompt": "", "tts_api_url": "", "tts_key_set": False,
                "tts_model": "qwen3-tts-flash", "tts_voice": "Cherry",
                "tts_language": "Chinese",
                "tts_format": "wav", "vision_model": "glm-4v-flash",
                "vision_api_url": "", "vision_key_set": False, "enable_thinking": False,
            }
        return {
            "configured": bool(config.api_url and config.model and config.key_set),
            "api_url": config.api_url,
            "model": config.model,
            "key_set": config.key_set,
            "system_prompt": config.system_prompt,
            "enable_thinking": config.enable_thinking,
            "tts_api_url": config.tts_api_url,
            "tts_key_set": config.tts_key_set,
            "tts_model": config.tts_model,
            "tts_voice": config.tts_voice,
            "tts_language": config.tts_language,
            "tts_format": config.tts_format,
            "vision_model": config.vision_model,
            "vision_api_url": config.vision_api_url,
            "vision_key_set": bool(config.vision_api_key),
        }
