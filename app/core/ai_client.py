"""AI 客户端 —— OpenAI 兼容接口的对话 + 流式 + 视觉。

对话与视觉都走"OpenAI 兼容"这一层，具体是哪家由配置决定（当前部署：
对话 = 阿里云百炼 qwen3.7-flash，视觉 = 智谱 glm-4v-flash）：
视觉单独配了 `vision_api_url` / `vision_api_key` 就用它，没配则沿用对话那组。

发声（TTS）不在这里：统一走 `app/voice/tts.py` → Qwen3-TTS-Flash（阿里云百炼）。
旧的 edge-tts / 本地 sherpa 音色 / 智谱 `/audio/speech` 三级降级链已全部移除；
这里只保留一个薄封装 `unified_tts_synthesize` 与最后的提示音兜底。
"""
from __future__ import annotations

import json
import logging
import math
import struct
import wave
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import requests

from ..config import DEFAULT_SYSTEM_PROMPT
from .ai_config import AIConfigStore

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    pass


def write_beep_tone(output_path: Path, text: str = "") -> bool:
    """TTS 全部失败时的本地降级：0.4s 880Hz 提示音 WAV。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate, duration = 16000, 0.4
    frames = int(sample_rate * duration)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 880 * i / sample_rate)))
                for i in range(frames)
            )
        )
    if text:
        logger.info("本地提示音代替 TTS：%s", text[:20])
    return True


def unified_tts_synthesize(text: str, output_path: Path) -> bool:
    """统一的 TTS 入口（见 app/voice/tts.py）：Qwen3-TTS-Flash，失败返回 False。

    这里保留一个薄封装，方便 AI 客户端与测试直接调用。
    """
    try:
        from ..voice.tts import synthesize
        return bool(synthesize(text, Path(output_path)))
    except Exception as e:
        logger.warning("TTS 合成失败(%s)", e)
        return False


def get_ai_client():
    """按优先级取 AI 客户端：环境变量 Key → 加密配置文件 → 本地规则回复。"""
    import os

    env_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if env_key:
        return OpenAICompatibleClient({
            "api_url": os.environ.get("ZHIPU_API_URL", "https://open.bigmodel.cn/api/paas/v4"),
            "model": os.environ.get("ZHIPU_MODEL", "glm-4-flash"),
            "api_key": env_key,
        })
    try:
        cfg = AIConfigStore().load()
        if cfg is not None and cfg.api_url and cfg.key_set:
            return OpenAICompatibleClient(cfg)
    except Exception as exc:
        logger.error("AI 客户端初始化失败，使用本地规则回复: %s", exc)
    return LocalEchoClient()


def get_vision_client():
    """视觉客户端（GLM-4V / Qwen-VL 等，未配置返回 None）。

    连接信息取配置里的 `vision_*`：单独配了就用它（对话换成百炼 Qwen 后，
    视觉仍可留在智谱 GLM-4V），没配则沿用对话那一组。
    """
    import os

    env_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if env_key:
        return OpenAICompatibleClient({
            "api_url": os.environ.get("ZHIPU_API_URL", "https://open.bigmodel.cn/api/paas/v4"),
            "model": os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash"),
            "vision_model": os.environ.get("ZHIPU_VISION_MODEL", "glm-4v-flash"),
            "api_key": env_key,
        })
    try:
        cfg = AIConfigStore().load()
        if cfg is None:
            return None
        api_url, api_key = cfg.vision_connection
        if not (api_url and api_key):
            return None
        return OpenAICompatibleClient({
            "api_url": api_url,
            "api_key": api_key,
            "model": cfg.vision_model or "glm-4v-flash",
            "vision_model": cfg.vision_model or "glm-4v-flash",
            "system_prompt": cfg.system_prompt,
        })
    except Exception as exc:
        logger.error("视觉客户端初始化失败: %s", exc)
    return None


class OpenAICompatibleClient:
    """AI Platform Chat Completions 兼容客户端（智谱 GLM / AI Platform / DeepSeek 等通用）。

    初始化：① 传参数字典（环境变量模式 / 视觉连接）② 不传参读配置文件。
    传了 image_path 的 chat() 走 vision_model，否则走 model。
    """

    def __init__(self, config: Any = None):
        if isinstance(config, dict):
            self.api_url = config["api_url"].rstrip("/")
            self.model = config["model"]
            self.api_key = config["api_key"]
            self.system_prompt = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
            self.vision_model = config.get("vision_model", "glm-4v-flash")
            self.enable_thinking = bool(config.get("enable_thinking", False))
            if not self.api_key:
                raise RuntimeError("AI API Key 未配置")
            return

        store = AIConfigStore()
        cfg = store.load()
        if cfg is None or not getattr(cfg, "api_url", ""):
            raise RuntimeError(
                "AI 尚未配置：请在 data_local/ai_config.json 里填 api_url / model / api_key，"
                "或设置环境变量 ZHIPU_API_KEY"
            )
        self.api_url = cfg.api_url.rstrip("/")
        self.model = cfg.model
        self.system_prompt = cfg.system_prompt or DEFAULT_SYSTEM_PROMPT
        self.vision_model = cfg.vision_model
        self.enable_thinking = cfg.enable_thinking
        # cfg.api_key 已在 load() 里解析好（明文优先，兼容旧的加密字段）
        self.api_key = cfg.api_key
        if not self.api_key:
            raise RuntimeError("AI API Key 未配置（在 data_local/ai_config.json 里填 api_key）")

    @staticmethod
    def _headers(api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _thinking_params(self) -> dict:
        """要不要带 `enable_thinking`。

        Qwen3 系模型默认先"思考"再回答：实测同一句话首字 16.7s、总 18.9s，
        关掉后首字 0.9s、总 3.6s —— 语音助手这个延迟不能接受，所以默认关。
        别的厂商（智谱 GLM 等）不认这个参数，**只在 qwen3 系模型上带**，
        免得换回 GLM 时被拒。（要开回来：ai_config.json 里 enable_thinking=true）
        """
        if "qwen3" in str(self.model).lower():
            return {"enable_thinking": bool(self.enable_thinking)}
        return {}

    @staticmethod
    def _image_data_url(image_path: Path) -> str:
        import base64

        suffix = Path(image_path).suffix.lower().lstrip(".") or "jpg"
        if suffix == "jpg":
            suffix = "jpeg"
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        return f"data:image/{suffix};base64,{b64}"

    def _build_messages(self, prompt: str, context: str | None = None, image_path: Path | None = None) -> list:
        messages = [{"role": "system", "content": self.system_prompt}]
        if context:
            messages.append({"role": "system", "content": f"以下是长期对话摘要与最近对话，供你参考：\n{context}"})
        if image_path:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": self._image_data_url(image_path)}},
                    {"type": "text", "text": prompt},
                ],
            })
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def chat(self, prompt: str, context: str | None = None, image_path: Path | None = None,
             max_tokens: int | None = None, temperature: float = 0.7,
             timeout: float = 60) -> str:
        """对话（可带图片 → 自动走视觉模型）；失败返回友好错误文本。

        `temperature` / `timeout` 是给"记忆摘要压缩"这类内部调用用的
        （`core/memory.py` 的 TextCompressor 协议要求这两个关键字）。
        """
        model = self.vision_model if image_path else self.model
        try:
            response = requests.post(
                f"{self.api_url}/chat/completions",
                headers=self._headers(self.api_key),
                json={"model": model, "messages": self._build_messages(prompt, context, image_path),
                      "temperature": temperature, **self._thinking_params(),
                      **({"max_tokens": max_tokens} if max_tokens else {})},
                timeout=timeout,
            )
            response.raise_for_status()
            reply = response.json()["choices"][0]["message"]["content"].strip()
            return reply or "（模型返回为空）"
        except Exception as e:
            logger.error("Chat失败: %s", e)
            return f"抱歉，AI 服务暂时不可用（{e}）。"

    def chat_stream(self, prompt: str, context: str | None = None,
                    max_tokens: int | None = None,
                    user_text: str | None = None) -> Iterator[str]:
        """流式对话：逐段 yield 文本增量（OpenAI 兼容 SSE）。失败时 yield 完整错误回复。

        user_text：用户原话（本地规则客户端据此匹配；真实模型用不到，忽略）。
        """
        try:
            response = requests.post(
                f"{self.api_url}/chat/completions",
                headers=self._headers(self.api_key),
                json={"model": self.model, "messages": self._build_messages(prompt, context),
                      # 温度略高一些：降低"每轮都一个腔调"的概率
                      "temperature": 0.85, "stream": True,
                      **self._thinking_params(),
                      "max_tokens": max_tokens if max_tokens is not None else 120},
                timeout=(10, 60),
                stream=True,
            )
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                except Exception:
                    continue
                if delta:
                    yield delta
        except Exception as e:
            logger.error("ChatStream失败: %s", e)
            yield f"抱歉，AI 服务暂时不可用（{e}）。"

    def tts(self, text: str, output_path: Path) -> bool:
        """TTS：Qwen3-TTS-Flash（app/voice/tts.py）→ 失败则本地提示音。"""
        output_path = Path(output_path)
        if unified_tts_synthesize(text, output_path):
            return True
        logger.warning("TTS 合成失败，降级为本地提示音")
        return write_beep_tone(output_path, text)


class LocalEchoClient:
    """未配置真实 AI 时的本地规则回复客户端（无网络依赖，也支持流式接口）。"""

    RULES = [
        (["压力", "紧张", "焦虑", "崩溃", "烦", "心情不好", "难过", "委屈"],
         "听起来你现在不太好受。我们先做三次深呼吸：吸气…呼气…先不做任何决定，"
         "也不用急着吃东西。愿意的话，跟我说说发生了什么？"),
        (["升职", "庆祝", "好消息", "开心", "高兴", "太棒"],
         "真为你高兴！庆祝的时候更要好好享受——选你真正想吃的，坐下来，一口一口慢慢品尝。"),
        (["糖尿病", "血糖", "高血压", "血压", "吃药", "胰岛素"],
         "正念饮食能帮你更好地觉察饥饿和饱足，但它不能替代药物。如果调整了进食方式，"
         "记得监测血糖血压，并和医生讨论这些变化。"),
        (["饱", "吃饱", "吃不下", "撑"],
         "能感受到饱足感是很棒的正念信号，七分饱是最舒服的状态，可以停下来了。"),
        (["饿", "饥饿", "想吃", "空腹"],
         "听起来你有点饿了。先感受一下：是胃在饿，还是心在饿？确认之后再慢慢开始吃。"),
        (["快", "着急", "赶时间", "来不及"],
         "吃太快身体来不及告诉你饱了。试着放下筷子，咀嚼到食物几乎化开再咽下。"),
        (["好吃", "享受", "香", "美味"],
         "能享受食物的味道真好，试着记住这一刻的感受，这一口是什么味道、什么口感？"),
        (["结束", "再见", "谢谢", "可以了"],
         "好的，再见。祝你用餐愉快，记得慢慢吃。"),
    ]

    DEFAULT_REPLY = "我在听。可以跟我说说你现在身体的感受吗？比如饿不饿、吃到几分饱了。"
    NOT_CONFIGURED_REPLY = (
        "好的，我听到了。当前未配置真实 AI（请在 data_local/ai_config.json 里填 API Key 与模型名），"
        "这是本地规则回复。"
    )

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def chat(self, prompt: str, context: str | None = None, image_path: Path | None = None,
             user_text: str | None = None) -> str:
        # 只匹配用户原话：prompt 里含状态机引导语（如"你真的感到饥饿吗"），
        # 拿整段 prompt 匹配会每轮都命中"饥饿"而答非所问。
        text = (user_text or prompt or "")
        for keywords, reply in self.RULES:
            if any(k in text for k in keywords):
                return reply
        if user_text:
            return "我在听。可以跟我说说你现在身体的感受吗？比如饿不饿、吃到几分饱了。"
        return self.NOT_CONFIGURED_REPLY

    def chat_stream(self, prompt: str, context: str | None = None,
                    max_tokens: int | None = None, user_text: str | None = None):
        yield self.chat(prompt, context, user_text=user_text)

    def tts(self, text: str, output_path: Path) -> bool:
        output_path = Path(output_path)
        if unified_tts_synthesize(text, output_path):
            return True
        return write_beep_tone(output_path, text)
