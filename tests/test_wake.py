"""语音唤醒的设置 / 接口 / 前端接线回归测试。

唤醒引擎已统一为 sherpa-onnx KWS（见 app/voice/sherpa_kws.py），
旧的 Vosk 方案（语法词表 + 拼音容错 + WakeDetector）已从工程中删除，
针对它的测试也一并移除 —— KWS 自身的测试在 tests/test_sherpa_kws.py。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"


# ---------------- 设置持久化与面板下发 ----------------
def test_wake_settings_roundtrip(tmp_path):
    from app.core.interaction_config import WAKE_CONTINUOUS, AgentSettingsStore

    path = tmp_path / "agent_settings.json"
    store = AgentSettingsStore(path)
    assert store.load().wake_enabled is True          # 设备屏默认开启
    assert "你好饭崽" in store.load().wake_words

    s = store.save(wake_enabled=False, wake_mode=WAKE_CONTINUOUS)
    assert s.wake_enabled is False and s.wake_mode == WAKE_CONTINUOUS
    assert AgentSettingsStore(path).load().wake_mode == WAKE_CONTINUOUS

    # 唤醒词清洗：去空白、去重、限量
    s = store.save(wake_words=[" 你好饭崽 ", "你好饭崽", "", "饭崽"])
    assert list(s.wake_words) == ["你好饭崽", "饭崽"]
    # 全部非法 → 回退默认词，绝不落成空表（否则唤醒永远失效）
    assert list(store.save(wake_words=[]).wake_words) == ["你好饭崽", "你好正念"]


def test_wake_panel_payload_exposes_modes_and_words():
    from app.core import interaction_config as ic

    payload = ic.panel_payload()
    keys = [m["key"] for m in payload["wake_modes"]]
    assert keys == [ic.WAKE_SINGLE, ic.WAKE_CONTINUOUS]
    assert payload["wake_chunk_ms"] > 0
    assert "再见" in payload["wake_stop_words"]
    settings = payload["settings"]
    for field in ("wake_enabled", "wake_mode", "wake_mode_name", "wake_words"):
        assert field in settings


def test_is_stop_phrase_only_matches_short_utterances():
    from app.core.interaction_config import is_stop_phrase

    assert is_stop_phrase("再见")
    assert is_stop_phrase("那我们先再见吧")
    assert not is_stop_phrase("我今天吃了米饭和青菜")
    assert not is_stop_phrase("")


# ---------------- 接口 ----------------
def test_wake_status_reports_kws_engine(tmp_path, monkeypatch):
    import asyncio

    import app.api.agent as agent_api
    from app.core import interaction_config as ic
    from app.voice.sherpa_kws import kws_detector

    monkeypatch.setattr(ic, "agent_settings_store", ic.AgentSettingsStore(tmp_path / "s.json"))

    status = asyncio.run(agent_api.wake_status())
    assert status["enabled"] is True
    assert status["words"]
    assert status["mode"] in (ic.WAKE_SINGLE, ic.WAKE_CONTINUOUS)
    # 只保留一个引擎，不再有 vosk 回退
    assert status["engine"] == "sherpa-kws"
    assert agent_api.active_wake_detector() is kws_detector


def test_wake_feed_empty_body_is_noop(tmp_path, monkeypatch):
    import asyncio

    import app.api.agent as agent_api
    from app.core import interaction_config as ic

    monkeypatch.setattr(ic, "agent_settings_store", ic.AgentSettingsStore(tmp_path / "s.json"))

    class FakeRequest:
        query_params = {"session": "t1"}

        async def body(self):
            return b""

    out = asyncio.run(agent_api.wake_feed(FakeRequest()))
    assert out["matched"] is False          # 空音频直接返回，不加载模型


def test_wake_settings_api_persists_and_drops_sessions(tmp_path, monkeypatch):
    import asyncio

    import app.api.agent as agent_api
    from app.core import interaction_config as ic
    from app.voice.sherpa_kws import kws_detector

    monkeypatch.setattr(ic, "agent_settings_store", ic.AgentSettingsStore(tmp_path / "s.json"))
    monkeypatch.setattr(agent_api, "apply_settings", lambda settings=None: None)
    kws_detector._sessions["keep"] = {"stream": object(), "words": ("x",),
                                      "last_hit": 0.0, "hit_times": []}

    class FakeRequest:
        async def json(self):
            return {"wake_enabled": False, "wake_mode": "continuous",
                    "wake_words": ["你好饭崽", "小饭崽"]}

    out = asyncio.run(agent_api.set_settings(FakeRequest()))
    assert out["settings"]["wake_enabled"] is False
    assert out["settings"]["wake_mode"] == "continuous"
    assert out["settings"]["wake_words"] == ["你好饭崽", "小饭崽"]
    # 改了唤醒词必须丢弃旧识别器（否则关键词词表过期）
    assert kws_detector._sessions == {}


def test_wake_feed_disabled_short_circuits(tmp_path, monkeypatch):
    import asyncio

    import app.api.agent as agent_api
    from app.core import interaction_config as ic

    monkeypatch.setattr(ic, "agent_settings_store", ic.AgentSettingsStore(tmp_path / "s.json"))
    ic.update_settings(wake_enabled=False)

    class FakeRequest:
        query_params = {}

        async def body(self):
            return b"\x00\x00" * 100

    out = asyncio.run(agent_api.wake_feed(FakeRequest()))
    assert out["matched"] is False and out["enabled"] is False


def test_wake_ack_text_is_configured_and_short():
    """唤醒后先回应一声「我在」：必须配好、且足够短（应答越短越快进入对话）。

    **句号是必须的**：实测 qwen3-tts-flash 对「我在」这种两字输入会"赶着收尾"，
    5 次里约 1 次在能量 85% 处直接掐断（听感就是"我在……"后面卡了半个字）。
    加句号后 5/5 自然收尾、时长稳定。所以这里断言必须带标点。
    """
    from app.core import interaction_config as ic

    ack = ic.WAKE["ack_text"]
    assert ack and isinstance(ack, str)
    assert ack.rstrip("。！？.!?") == "我在", f"应答语必须是「我在」：{ack!r}"
    assert ack.endswith(("。", "！", "？")), (
        f"应答语必须以句末标点结尾，否则 TTS 可能把尾音掐掉：{ack!r}")
    assert len(ack) <= 6, "应答语过长会拖慢进入聆听"


def test_wake_ack_endpoint_prefers_cache(monkeypatch):
    """应答语音优先走 TTS 缓存（启动已预热），命中缓存时不再合成。"""
    import asyncio
    import base64

    import app.api.agent as agent_api
    from app.voice import tts_cache

    fake_audio = b"RIFF-fake-wav-bytes"
    monkeypatch.setattr(tts_cache, "get", lambda text, voice, rate=None: fake_audio)
    calls = {"synth": 0}

    def fake_synth(text, out, voice=None, rate=None):
        calls["synth"] += 1
        out.write_bytes(b"freshly-synthesized")
        return True

    monkeypatch.setattr("app.voice.tts.synthesize", fake_synth)

    out = asyncio.run(agent_api.wake_ack())
    assert out["ok"] is True
    assert out["text"] == "我在。"
    assert base64.b64decode(out["audio_b64"]) == fake_audio
    assert calls["synth"] == 0, "命中缓存时不应再合成"


def test_wake_ack_endpoint_synthesizes_when_cache_miss(monkeypatch):
    """缓存未命中时现场合成；合成失败要安全降级（ok=False，前端不卡住）。"""
    import asyncio
    import base64

    import app.api.agent as agent_api
    from app.voice import tts_cache

    monkeypatch.setattr(tts_cache, "get", lambda text, voice, rate=None: None)
    monkeypatch.setattr(tts_cache, "put", lambda *a, **k: None)

    def ok_synth(text, out, voice=None, rate=None):
        out.write_bytes(b"RIFF-fresh-audio")
        return True

    monkeypatch.setattr("app.voice.tts.synthesize", ok_synth)
    out = asyncio.run(agent_api.wake_ack())
    assert out["ok"] is True
    assert base64.b64decode(out["audio_b64"]) == b"RIFF-fresh-audio"

    def failing_synth(text, out, voice=None, rate=None):
        return False

    monkeypatch.setattr("app.voice.tts.synthesize", failing_synth)
    failed = asyncio.run(agent_api.wake_ack())
    assert failed["ok"] is False, "合成失败要返回 ok=False，让前端直接进入聆听而不是卡住"


def _wav(seconds: float, amp: float = 0.3, rate: int = 24000,
         tail_silence: float = 0.0) -> bytes:
    """造一段 WAV：前段正弦 +（可选）尾部静音，用于测"收干净了没有"。"""
    import io
    import math
    import struct
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * seconds)
        w.writeframes(b"".join(
            struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(n)))
        if tail_silence:
            w.writeframes(b"\x00\x00" * int(rate * tail_silence))
    return buf.getvalue()


def test_wake_ack_rejects_rushed_render_and_retries(monkeypatch):
    """唤醒应答必须挑"收干净了"的那一条。

    实测 qwen3-tts-flash 对「我在」这种两字输入会赶着收尾：同一句话能在
    320ms~960ms 之间乱跳，偶尔直接把尾音掐断（听感就是"我在……后面卡了半个字"）。
    唤醒应答每次唤醒都要播，一旦把残句预热进缓存，用户就永远听到那句残句。
    """
    from app.voice import tts_cache

    # 第一次给"掐断的残句"（短 + 句尾还在出声），第二次给正常的一条
    rushed = _wav(0.30, amp=0.3)                 # 320ms，没有尾部静音
    good = _wav(0.56, amp=0.3, tail_silence=0.08)  # 560ms + 80ms 静音
    calls = {"n": 0}

    def fake_synth(text, out, voice=None, rate=None):
        calls["n"] += 1
        out.write_bytes(rushed if calls["n"] == 1 else good)
        return True

    monkeypatch.setattr("app.voice.tts.synthesize", fake_synth)
    monkeypatch.setattr(tts_cache, "_CACHE", type(tts_cache._CACHE)())

    picked = tts_cache.prewarm_ack("我在。", tries=2)
    assert calls["n"] == 2, "第一遍没达标就该重试"
    assert picked == good
    assert not tts_cache._ack_ok(tts_cache.wav_stats(rushed)), "掐断的样本不该被接受"
    assert tts_cache._ack_ok(tts_cache.wav_stats(good))
    # 挑出来的那条会被缓存，下次唤醒直接命中（唤醒必须秒回）
    assert tts_cache.get("我在。", tts_cache._default_voice()) == good


def test_wake_ack_keeps_best_when_nothing_is_ideal(monkeypatch):
    """全都达不到理想质感时，取其中最好的一条 —— 有声音总比唤醒后一声不响强。"""
    from app.voice import tts_cache

    a = _wav(0.30, amp=0.3)
    b = _wav(0.45, amp=0.3, tail_silence=0.06)
    calls = {"n": 0}

    def fake_synth(text, out, voice=None, rate=None):
        calls["n"] += 1
        out.write_bytes(a if calls["n"] == 1 else b)
        return True

    monkeypatch.setattr("app.voice.tts.synthesize", fake_synth)
    monkeypatch.setattr(tts_cache, "_CACHE", type(tts_cache._CACHE)())
    assert tts_cache.prewarm_ack("我在。", tries=2) == b


# ---------------- 前端接线（静态断言） ----------------
def test_device_page_wires_wake_word_controls_and_api():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "device_ui.css").read_text(encoding="utf-8")

    for element in ('id="setWake"', 'id="wakeSeg"', 'id="wakeWords"', 'id="wakeDesc"'):
        assert element in html
    assert "你好饭崽" in html                      # 底部提示要让用户知道怎么说

    assert "startWakeListen" in js and "stopWakeListen" in js
    assert "/api/agent/wake/feed" in js and "/api/agent/wake/reset" in js
    assert "resumeAfterTurn" in js and "wakeIsStop" in js
    # 唤醒监听与录音不能同时持有麦克风
    assert "if (_wakeOn) stopWakeListen()" in js
    # 唤醒后先应答「我在」，播完才开麦收音：顺序反了会把应答本身录进去
    assert "/api/agent/wake/ack" in js
    assert "playWakeAck().finally(" in js
    assert "_wakeAckPlaying" in js
    assert ".switch" in css


def test_frontend_noise_gate_and_turn_mutex():
    """抗噪与"防插话"的前端逻辑必须在位（两条都是实测出来的真问题）。"""
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")

    # 抗噪第一道闸：唤醒只上传"有声音"的片段（静音不发请求、噪声不送去解码）
    assert "_wakeFloor" in js and "_wakeUploading" in js and "_wakePre" in js
    # 断句阈值必须留够：0.6s 会把中文自然换气停顿切成一前一后两轮
    assert "silenceMs > 850" in js
    # 真正说话太短（咳嗽/杂音）→ 判为噪声，不送识别
    assert "_vadSpeechMs < 450" in js
    # 但仅当静音检测确实在工作时才敢丢（后台标签页 rAF 被节流，检测没跑）
    assert "_vadReady" in js
    # 唤醒必须在任何自主互动模式下都可用：播报/生成期间**不屏蔽**唤醒，
    # 而是走"打断"（abortDialogue），否则机器人一开口就叫不醒。
    assert "if (_recording || _sheetOpen) return;" in js
    assert "if (_dlgBusy || _playing) abortDialogue();" in js
    assert "AbortController" in js and "_dlgAbort" in js
    # 两路对话互斥：打断后旧轮的 finally 不得清掉新轮的标志（靠轮次编号）
    assert "_dlgGen" in js and "if (gen === _dlgGen)" in js
    # 唤醒监听的可用条件里不能再包含"正在播报"这类否决项
    assert "!wakeEnabled()" not in js.split("function wakeCanListen()")[1][:220]


def test_frontend_tap_can_interrupt_and_end_continuous_session():
    """点屏幕也能结束/打断对话；打断后不再自动续连续对话。"""
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    tap = js.split("function onTap()")[1][:900]
    # 说话/生成/还有音频排队时，点屏幕 = 打断
    assert "_playing || _dlgBusy || _audioQueue.length" in tap
    assert "abortDialogue()" in tap
    assert "_wakeSessionActive = false" in tap, "打断必须结束本轮会话，不能自动续"
    # 空闲时点屏幕开始说话，并入一轮新的会话（是否连续由设置决定）
    assert "_wakeSessionActive = true" in tap


def test_continuous_mode_keeps_listening_until_real_silence():
    """连续模式：说完一句**接着听**，只有"安静够久"才回到待唤醒。

    以前的做法是"每轮盲录 8 秒、连续 3 次没听到人声就退出"：用户吃着饭停一会儿，
    连续对话就自己没了 —— 体感上跟单句模式没区别（用户报的"切换不生效"）。
    现在：连续模式靠唤醒监听里的"听到人声"起手（不必再喊唤醒词），
    结束条件统一交给 WAKE.continuous_idle_sec。
    """
    from app.core import interaction_config as ic

    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")

    assert "let _waitingNextTurn" in js and "_continuousIdleSec" in js
    # 连续模式的续听不再立刻盲录，而是把监听挂回去等用户开口
    resume = js.split("async function resumeAfterTurn()")[1][:900]
    assert "_waitingNextTurn = true" in resume and "startWakeListen()" in resume
    assert "startRecording()" not in resume, "连续模式不该立刻盲录 8 秒"
    # 唤醒监听里听到人声 → 直接开始收音（不用再喊唤醒词）
    monitor = js.split("const speaking = level > gate;")[1][:900]
    assert "_waitingNextTurn" in monitor and "startRecording()" in monitor
    # "等着你接下一句"时门槛必须压低：用户一直说话时，自适应统计窗口里 20 分位样本
    # 全是他的声音，门会被抬到"音量 × 1.8"，于是永远等不到人开口（实测只升不降）
    assert "const gate = _waitingNextTurn ? Math.min(_wakeFloor, 6) : _wakeFloor;" in js
    # 麦克风有没有被占用看 _micOn（真的开着），不能看 _recording（"本轮还没收尾"，
    # 会一直挂到识别/生成/播报结束）——否则整段播报期间监听都挂不回去，
    # 用户看到机器人说完马上接一句，这几秒是聋的
    can = js.split("function wakeCanListen()")[1][:400]
    assert "!_micOn" in can and "!_recording" not in can
    # 录音一停（麦克风空出来）就把监听挂回去，不等整轮收尾
    assert "startWakeListen()" in js.split("_micOn = false; holdInputFace(false);")[1][:220]
    # 旧的"3 次没听到就退出"已经删掉
    assert "_noSpeechStreak" not in js
    assert ic.WAKE["continuous_idle_sec"] > 0
    assert ic.panel_payload()["wake_continuous_idle_sec"] == ic.WAKE["continuous_idle_sec"]


def test_frontend_reports_voice_so_ai_wont_cut_in():
    """听到人声要先"置忙"，AI 才不会在你说话时插嘴（自主互动的硬闸）。"""
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")

    assert "function voiceActive()" in js and "function noteVoiceHeard()" in js
    busy = js.split("function reportBusy()")[1][:400]
    assert "voiceActive()" in busy, "忙碌判定要把'听到人声'算进去"
    assert "return 'voice'" in js
    # 应答播完到开麦之间留静默期：立刻切麦克风会把应答尾音掐掉
    assert "ACK_SETTLE_MS" in js and "ACK_SETTLE_MS" in js.split("playWakeAck().finally")[1][:200]
    # 面板关闭时重新评估（面板开着时 wakeCanListen() 恒为假，容易留下"聋"的设备）
    close = js.split("function closeSheet()")[1][:300]
    assert "applyWakeSetting()" in close
    # 用户正在说话时不播 AI 的主动开口
    proactive = js.split("es.addEventListener('proactive'")[1][:400]
    assert "if (_recording) return;" in proactive


def test_wake_continuous_mode_setting_is_authoritative():
    """「单句 / 连续」必须以**设置**为准，且中途改动立即生效。

    这个 bug 真出现过：以前只用一个 _wakeContinuous 同时表示"会话进行中"和
    "当前是连续模式"，且只在唤醒那一刻赋值 —— 于是用户把设置从「连续」改成
    「单句」时它仍是 true，表现就是"切换不生效"。
    现在拆成：_wakeSessionActive（会话是否进行中，运行时）
             + wakeModeChoice()（设置，每次现读）
             = wakeContinuousNow()
    """
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")

    # 组合式判定必须存在，并且现读设置
    assert "function wakeContinuousNow()" in js
    body = js.split("function wakeContinuousNow()")[1][:200]
    assert "_wakeSessionActive" in body and "wakeModeChoice()" in body, body

    # 运行时标志与设置标志必须是两个不同的变量
    assert "let _wakeSessionActive" in js
    assert "let _wakeContinuous" not in js, "旧的合并标志应已删除"

    # 设置改动后要能立刻停掉正在进行的连续会话
    apply = js.split("function applyWakeSetting()")[1][:600]
    assert "wakeContinuousNow()" in apply
    assert "startWakeListen()" in apply

    # 连续的续听判断必须用组合判定，而不是缓存的布尔值
    resume = js.split("async function resumeAfterTurn()")[1][:700]
    assert "wakeContinuousNow()" in resume
    assert "_wakeSessionActive = false" in resume, "单句模式说完就该结束会话"

