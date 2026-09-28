"""pytest 全局夹具：隔离真实数据文件 + 断掉真实付费 API 调用。

两个目的：

1. **别改坏用户的真实配置**
   测试里只要实例化一次 `app.server.create_app()`（TestClient 会跑 startup），
   或者顺手调一次 `ic.update_settings(...)`，就会写到 <项目>/data_local/ 下的
   **真实** agent_settings.json / ai_config.json。曾出现过跑完测试后
   模式被改成「日常聊天」（于是摄像头被故意关掉）、自主互动变「关闭」。
   做法：把两个存储的**默认路径**改到临时目录，但显式传路径时仍尊重传入值
   （很多测试自己用 tmp_path 构造 store，不能把它们也劫持掉）。

2. **别打真实的付费接口**
   发声是云端 Qwen3-TTS（按字符计费），对话是云端 GLM。测试若拿真实 Key 跑，
   既花钱又不稳定。这里在**网络层**（requests）接一个只在本地生成 WAV 的假实现，
   于是 `qwen_tts_engine.synthesize()` 本身仍走完整逻辑（含归一化、头部修正、
   熔断），只是不会出网；单个测试若自己 monkeypatch `requests` 会覆盖这里。
"""
from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

import pytest

_TMPDIR = None


def _silent_wav(seconds: float = 0.6, rate: int = 24000) -> bytes:
    """生成一段合法的 16bit 单声道 WAV（带真实的 RIFF/data 长度）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_data():
    global _TMPDIR
    _TMPDIR = tempfile.TemporaryDirectory(prefix="mindful-test-data-")
    tmp = Path(_TMPDIR.name)

    from app.core import ai_config, interaction_config

    # ---- AI 配置：默认路径指向临时目录，并塞一把假 Key（让发声代码路径能跑起来）----
    orig_ai_cls = ai_config.AIConfigStore

    class _IsolatedAIConfigStore(orig_ai_cls):
        def __init__(self, config_path=None, key_path=None):
            super().__init__(config_path or (tmp / "ai_config.json"),
                             key_path or (tmp / "ai_config.key"))

    ai_config.AIConfigStore = _IsolatedAIConfigStore
    _IsolatedAIConfigStore().save(
        api_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        api_key="sk-test-chat",
        tts_api_key="sk-test-tts",
    )

    # ---- 设备设置 ----
    orig_agent_cls = interaction_config.AgentSettingsStore

    class _IsolatedAgentSettingsStore(orig_agent_cls):
        def __init__(self, path=None):
            super().__init__(path or (tmp / "agent_settings.json"))

    interaction_config.AgentSettingsStore = _IsolatedAgentSettingsStore
    interaction_config.agent_settings_store = _IsolatedAgentSettingsStore()
    interaction_config.get_settings.__globals__["agent_settings_store"] = \
        interaction_config.agent_settings_store

    yield

    ai_config.AIConfigStore = orig_ai_cls
    interaction_config.AgentSettingsStore = orig_agent_cls
    _TMPDIR.cleanup()
    _TMPDIR = None


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """把云端发声接口换成"本地生成 WAV"的假实现 —— 测试绝不真的出网花钱。

    只拦 Qwen3-TTS 的合成端点；对话模型（GLM）在测试里本来就走 LocalEchoClient
    或已被各自的用例 stub 掉。
    """
    import app.voice.qwen_tts_engine as eng

    wav = _silent_wav()

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        if "multimodal-generation/generation" not in str(url):
            raise AssertionError(f"测试里不应真的请求网络：POST {url}")
        # 与真实返回同构：data 为空串、音频在 url（且没有 status_code 字段）
        return _FakeResponse({
            "output": {"audio": {"data": "", "url": "https://fake.local/tts.wav"},
                       "finish_reason": "stop"},
            "usage": {"characters": len(str((json or {}).get("input", {}).get("text", "")))},
        })

    def fake_get(url, timeout=None, **kw):
        if str(url) != "https://fake.local/tts.wav":
            raise AssertionError(f"测试里不应真的请求网络：GET {url}")
        return _FakeResponse(content=wav)

    monkeypatch.setattr(eng.requests, "post", fake_post)
    monkeypatch.setattr(eng.requests, "get", fake_get)
    eng.reset_breaker()
    yield
    eng.reset_breaker()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每个测试开头清掉设置缓存，避免上一个测试留下的模式影响下一个。"""
    from app.core import interaction_config

    store = interaction_config.agent_settings_store
    store._cache = None
    yield
