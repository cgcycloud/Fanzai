"""sherpa-onnx 语音栈回归测试：KWS 唤醒 / 本地 TTS / 引擎选择。

背景：本项目原本用 Vosk-small 做唤醒（语法词表 + 拼音容错），
但该模型词表里没有「崽」—— 语法限制失效、自由识别把「饭崽」听成「贩灾」。
现已全面改用 sherpa-onnx（k2-fsa）：KWS（唤醒）+ Paraformer（识别）+ VITS（合成）。
"""
from __future__ import annotations

import io
import struct
import wave
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"


# ---------------- 关键词生成：中文 → 拼音 token 行 ----------------
def test_keyword_line_generates_pinyin_tokens():
    """「你好饭崽」→ `n ǐ h ǎo f àn z ǎi @你好饭崽`（模型的 token 就是声母+韵母）。"""
    from app.voice.sherpa_kws import keyword_line

    line, missing = keyword_line("你好饭崽")
    assert line.endswith("@你好饭崽")
    tokens = line.split("@")[0].split()
    assert tokens == ["n", "ǐ", "h", "ǎo", "f", "àn", "z", "ǎi"], line
    assert missing == [], "模型词表里应包含这些 token"


def test_keyword_line_handles_zero_initial_syllable():
    """零声母音节（爱 ài）整段作为一个 token —— 与官方 keywords.txt 写法一致。"""
    from app.voice.sherpa_kws import keyword_line

    line, missing = keyword_line("小爱同学")
    tokens = line.split("@")[0].split()
    assert tokens == ["x", "iǎo", "ài", "t", "óng", "x", "ué"], line
    assert missing == []


def test_keyword_line_supports_the_old_wake_words_too():
    """旧唤醒词「你好正念」和两字词「饭崽」都要能编码（Vosk 做不到两字词）。"""
    from app.voice.sherpa_kws import keyword_line

    for word in ("你好正念", "饭崽"):
        line, missing = keyword_line(word)
        assert line.endswith("@" + word) and not missing, (word, line, missing)


def test_keywords_text_reports_unencodable_words(monkeypatch):
    """无法转拼音/含词表外 token 的唤醒词应被跳过并报告，而不是生成坏行。"""
    import app.voice.sherpa_kws as kws

    text, skipped = kws.keywords_text(["你好饭崽", "", "!!!"])
    assert "你好饭崽" in text
    assert "" in skipped and "!!!" in skipped

    # 词表里没有的 token → 整词跳过（否则该行永远无法命中，还占着资源）
    monkeypatch.setattr(kws, "_tokens", lambda: {"n", "ǐ"})
    text2, skipped2 = kws.keywords_text(["你好饭崽"])
    assert text2 == "" and skipped2 == ["你好饭崽"]


# ---------------- 检测器 ----------------
def test_kws_detector_reports_unavailable_without_model(monkeypatch):
    """模型缺失时 feed 要安全返回（不抛异常），API 层据此回退 Vosk。"""
    import app.voice.sherpa_kws as kws

    monkeypatch.setattr(kws, "available", lambda: False)
    det = kws.KwsDetector()
    det._spotter = None
    out = det.feed("s", b"\x00\x00" * 100, words=["你好饭崽"])
    assert out["matched"] is False and out["reason"] in ("no-model", "empty")


def test_kws_detector_empty_input_is_noop():
    from app.voice.sherpa_kws import KwsDetector

    det = KwsDetector()
    out = det.feed("s", b"", words=["你好饭崽"])
    assert out["matched"] is False and out["reason"] == "empty"


@pytest.mark.skipif(not (ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01").exists(),
                    reason="KWS 模型未下载")
def test_kws_model_files_can_load():
    from app.voice import sherpa_kws

    assert sherpa_kws.available() is True
    assert sherpa_kws.kws_detector.warmup() is True
    status = sherpa_kws.kws_detector.status()
    assert status["engine"] == "sherpa-kws" and status["loaded"] is True


def test_real_speech_wakes_and_unrelated_speech_does_not():
    """真实语音回环：KWS 必须被「你好饭崽」唤醒，且不被无关语音误触发。"""
    from app.voice.sherpa_kws import kws_detector

    if not kws_detector.available():
        pytest.skip("KWS 模型未下载")
    ps = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not ps.exists():
        pytest.skip("非 Windows 环境，无中文语音合成音色")

    import wave

    import numpy as np

    from app.voice.convert import to_16k_wav

    def speech_pcm(text: str, tag: str):
        raw = Path(rf"C:\Users\hp\AppData\Local\Temp\pytest_kws_{tag}.wav")
        subprocess.run([str(ps), "-NoProfile", "-Command",
                        "Add-Type -AssemblyName System.Speech; "
                        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=0; "
                        f"$s.SetOutputToWaveFile('{raw}'); $s.Speak('{text}'); $s.Dispose()"],
                       capture_output=True)
        if not raw.exists():
            return None
        conv = raw.with_name(f"pytest_kws_{tag}_16k.wav")
        conv.write_bytes(to_16k_wav(raw.read_bytes()))
        with wave.open(str(conv), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    def detect(pcm, session):
        step = int(16000 * 0.2)
        for i in range(0, len(pcm), step):
            r = kws_detector.feed(session, pcm[i:i + step].tobytes(),
                                  words=["你好饭崽", "你好正念"], cooldown_sec=1.0)
            if r.get("matched"):
                return True, r.get("word")
        return False, ""

    pos = speech_pcm("你好饭崽", "pos")
    neg = speech_pcm("今天中午吃了红烧肉和米饭", "neg")
    if pos is None or neg is None:
        pytest.skip("系统无中文语音合成音色")

    matched, word = detect(pos, "pos")
    assert matched, "「你好饭崽」未能唤醒"
    assert word == "你好饭崽"
    assert not detect(neg, "neg")[0], "无关语音被误唤醒"


# ---------------- 本地 TTS ----------------
def test_detect_format_distinguishes_wav_and_mp3():
    from app.voice.tts import detect_format, mime_of

    assert detect_format(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert detect_format(b"ID3\x03\x00") == "mp3"
    assert detect_format(b"\xff\xfb\x90\x00") == "mp3"
    assert detect_format(b"") == "mp3"
    assert mime_of("wav") == "audio/wav" and mime_of("mp3") == "audio/mpeg"


def test_tts_cache_voice_key_includes_model_and_voice():
    """缓存键必须含模型与音色：否则换模型/音色后会命中旧的音频。

    这个 bug 真出现过：预热用的是旧引擎的键、对话查的是新引擎的键 →
    即时回应一直不播（缓存永远读不到）。
    """
    from app.voice import qwen_tts_engine
    from app.voice.tts import ENGINE_QWEN, cache_voice

    key = cache_voice()
    assert key.startswith(ENGINE_QWEN + ":"), key
    parts = key.split(":")
    assert len(parts) == 3, f"缓存键应为 引擎:模型:音色 -> {key}"
    assert qwen_tts_engine.model_name() in key
    assert qwen_tts_engine.default_voice() in key


def test_tts_engine_is_qwen_only():
    """发声只剩一个引擎：Qwen3-TTS-Flash。旧的本地音色与 edge 都不该再出现。"""
    import app.voice.tts as tts

    assert tts.ENGINE_QWEN == "qwen3-tts"
    opts = tts.engine_options()
    assert [o["key"] for o in opts] == [tts.ENGINE_QWEN]
    # 不再有多说话人概念
    assert tts.speaker_options() == []
    assert tts.speaker_count() == 1


def test_tts_reports_unavailable_when_not_configured(monkeypatch, tmp_path):
    """没配 API Key / 熔断中 → 明确报 unavailable，且 synthesize 返回 False 不抛异常。

    调用方据此降级为本地提示音，绝不静默无声。
    """
    import app.voice.tts as tts
    from app.voice import qwen_tts_engine

    monkeypatch.setattr(qwen_tts_engine, "available", lambda: False)
    assert tts.active_engine() == "unavailable"
    assert tts.available_engines() == []
    assert tts.synthesize("测试", tmp_path / "none.wav") is False


def test_qwen_request_shape_and_audio_download(monkeypatch, tmp_path):
    """按官方契约发请求：multimodal-generation 端点 + input.voice/language_type。

    非流式返回的 audio.data 是空串、音频在 audio.url，必须再下载一次。
    这里用假 requests 验证这两步都真的发生了。
    """
    import app.voice.qwen_tts_engine as eng

    monkeypatch.setattr(eng, "_settings", lambda: {
        "api_base": "https://dashscope.aliyuncs.com/api/v1",
        "model": "qwen3-tts-flash",
        "voice": "Cherry",
        "language_type": "Chinese",
        "api_key": "sk-test",
    })

    seen = {}

    class FakeResp:
        # HTTP 层的 status_code 真实存在；当年搞错的是 **JSON body 里的**那个同名字段
        status_code = 200
        text = ""

        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        # 关键：真实成功响应里**没有** status_code 字段（文档示例里有，实际没有）。
        # 早期 mock 照文档写了 status_code=200，于是代码里"必须等于 200"的错误校验
        # 一直没被测试发现，线上所有合法响应都被判失败。
        return FakeResp({"output": {"audio": {"data": "", "url": "https://cdn.example/a.wav"}}})

    def fake_get(url, timeout=None):
        seen["download"] = url
        return FakeResp(content=_streamed_wav())

    monkeypatch.setattr(eng.requests, "post", fake_post)
    monkeypatch.setattr(eng.requests, "get", fake_get)

    out = tmp_path / "q.wav"
    assert eng.synthesize("你好", out) is True
    assert seen["url"].endswith("/services/aigc/multimodal-generation/generation")
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["json"]["model"] == "qwen3-tts-flash"
    assert seen["json"]["input"]["text"] == "你好"
    assert seen["json"]["input"]["voice"] == "Cherry"
    assert seen["json"]["input"]["language_type"] == "Chinese"
    assert seen["download"] == "https://cdn.example/a.wav"
    assert out.stat().st_size > 512


def _streamed_wav(frames: int = 2400, rate: int = 24000) -> bytes:
    """构造一个"流式 WAV"：数据正常，但 RIFF/data size 是占位值。

    这正是 DashScope 实际返回的形态（RIFF size 写成 0x7FFFFEFF），
    必须被 normalize_wav_header 修正，否则浏览器算出的时长是 12 小时。
    """
    import numpy as np
    pcm = (np.sin(np.linspace(0, 60, frames)) * 8000).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    raw = bytearray(buf.getvalue())
    struct.pack_into("<I", raw, 4, 0x7FFFFEFF)          # RIFF size 占位
    data_at = raw.index(b"data")
    struct.pack_into("<I", raw, data_at + 4, 0x7FFFFEFF)  # data size 占位
    return bytes(raw)


def test_normalize_wav_header_fixes_placeholder_sizes():
    """流式 WAV 的占位长度必须被改写成真实长度（幂等）。"""
    from app.voice.qwen_tts_engine import normalize_wav_header

    raw = _streamed_wav()
    # 修正前：wave 读出的时长是错的（远超实际）
    with wave.open(io.BytesIO(raw), "rb") as w:
        bogus = w.getnframes()
    assert bogus > 1_000_000, f"占位头应被构造成离谱的帧数，实际 {bogus}"

    fixed = normalize_wav_header(raw)
    assert len(fixed) == len(raw), "只改头部，不得改动音频数据"
    assert fixed[44:] == raw[44:], "PCM 数据必须原样保留"
    assert struct.unpack_from("<I", fixed, 4)[0] == len(fixed) - 8
    with wave.open(io.BytesIO(fixed), "rb") as w:
        assert w.getnframes() == 2400
        assert w.getnframes() * 2 + 44 == len(fixed)

    # 幂等：再跑一次不变
    assert normalize_wav_header(fixed) == fixed
    # 非 WAV 输入原样返回，不抛异常
    assert normalize_wav_header(b"not a wav") == b"not a wav"
    assert normalize_wav_header(b"") == b""


def test_normalize_base_accepts_compatible_mode_url():
    """Base URL 容错：用户很容易把 compatible-mode（对话端点）填进来。

    实测该 URL 拼出 TTS 路径会 404，且**响应体为空**，用户完全看不出原因。
    同一个 host 上 TTS 路径固定，所以直接归一，而不是让用户对着空 body 猜。
    """
    from app.voice.qwen_tts_engine import normalize_base

    want = "https://dashscope.aliyuncs.com/api/v1"
    for raw in ("https://dashscope.aliyuncs.com/compatible-mode/v1",
                "https://dashscope.aliyuncs.com/compatible-mode/v1/",
                "https://dashscope.aliyuncs.com",
                "https://dashscope.aliyuncs.com/",
                "",
                None):
        assert normalize_base(raw) == want, f"{raw!r} 应归一到 {want}"

    # 已经是正确形态 → 保持不变
    assert normalize_base(want) == want
    full = want + "/services/aigc/multimodal-generation/generation"
    assert normalize_base(full) == full
    # 自建/代理 host 不做猜测
    assert normalize_base("https://my-proxy.internal/tts/v1") == "https://my-proxy.internal/tts/v1"


def test_qwen_rejects_json_error_with_code():
    """接口在 JSON 里报错（带 code）时必须判失败，不能当成成功。"""
    import app.voice.qwen_tts_engine as eng

    eng.reset_breaker()
    orig_settings, orig_post = eng._settings, eng.requests.post
    try:
        eng._settings = lambda: {"api_base": "https://dashscope.aliyuncs.com/api/v1",
                                 "model": "qwen3-tts-flash", "voice": "Cherry",
                                 "language_type": "Chinese", "api_key": "sk-x"}

        class R:
            status_code = 200
            text = ""

            def json(self):
                return {"code": "InvalidApiKey", "message": "bad key"}

        eng.requests.post = lambda *a, **k: R()
        assert eng.synthesize("测试", Path("data_local/media/audio/_x.wav")) is False
        assert "InvalidApiKey" in eng.status()["last_error"]
    finally:
        eng._settings, eng.requests.post = orig_settings, orig_post
        eng.reset_breaker()
        Path("data_local/media/audio/_x.wav").unlink(missing_ok=True)


def test_qwen_breaker_opens_after_repeated_failure(monkeypatch):
    """连续失败要熔断：避免每句话都白等一次网络超时。"""
    import app.voice.qwen_tts_engine as eng

    eng.reset_breaker()
    assert eng.blocked() is False
    for _ in range(eng.FAILS_BEFORE_DOWN):
        eng.note_failure("boom")
    assert eng.blocked() is True
    eng.reset_breaker()
    assert eng.blocked() is False


def test_qwen_speed_conversion_matches_legacy_percent():
    """语速口径沿用旧 edge-tts 的百分比：+15% → 1.15 倍速，语义不变。"""
    from app.voice.qwen_tts_engine import _speed_from_rate

    assert abs(_speed_from_rate(0) - 1.0) < 1e-9
    assert abs(_speed_from_rate(15) - 1.15) < 1e-9
    assert abs(_speed_from_rate("-10%") - 0.90) < 1e-9
    # 夹取边界，避免极端值把音频拉坏
    assert _speed_from_rate(500) == 2.0
    assert _speed_from_rate(-500) == 0.5


def test_time_stretch_preserves_format_and_changes_length():
    """本地变速：改变样本数（时长）但保持 WAV 头/声道/采样率不变。"""
    import io
    import wave

    import numpy as np

    from app.voice.qwen_tts_engine import audio_time_stretch

    frames = 1600
    pcm = (np.sin(np.linspace(0, 40, frames)) * 8000).astype("<i2").tobytes()
    faster = audio_time_stretch(pcm, 16000, 1, 2, 2.0)
    slower = audio_time_stretch(pcm, 16000, 1, 2, 0.5)
    assert len(faster) < len(pcm) < len(slower)
    # 1.0 倍速应当原样返回
    assert audio_time_stretch(pcm, 16000, 1, 2, 1.0) == pcm
    # 仍是合法 PCM：样本数可被 2 整除
    assert len(faster) % 2 == 0 and len(slower) % 2 == 0

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(slower)
    with wave.open(io.BytesIO(buf.getvalue()), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1


# ---------------- 前后端接线 ----------------
def test_api_exposes_kws_wake_engine():
    """/api/agent/wake 要报出实际引擎（现在只有 KWS，不再有 Vosk 回退）。"""
    import asyncio

    import app.api.agent as agent_api
    from app.voice.sherpa_kws import kws_detector

    status = asyncio.run(agent_api.wake_status())
    assert status["engine"] == "sherpa-kws"
    assert "engines" not in status, "Vosk 回退已删除，不应再暴露多引擎状态"
    if kws_detector.ready():
        assert agent_api.active_wake_detector() is kws_detector


def test_frontend_uses_audio_format_for_mime():
    """前端必须按容器格式选 MIME：本地 TTS 出 WAV，写死 audio/mpeg 会播不出来。"""
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "function mimeOf(" in js and "audio/wav" in js
    assert "playB64(d.audio_b64, d.format)" in js
    assert "mimeOf(_wakeAckFmt)" in js


def test_frontend_handles_autoplay_block():
    """浏览器自动播放被拦时不能静默丢音频（曾因此"有文字没声音"且查不到原因）。"""
    js = (STATIC / "js" / "device_ui.js").read_text(encoding="utf-8")
    assert "NotAllowedError" in js
    assert "_audioQueue.unshift(item)" in js, "被拦下的音频必须放回队列，不能丢"
    assert "armAudioUnlock" in js and "_audioUnlocked" in js
    assert "点一下屏幕开启声音" in js
    # 无声提示优先于其它状态栏文案（否则会被状态轮询覆盖，用户看不到指引）
    assert "if (_audioBlocked) {" in js
    # 解码/播放失败要有可见反馈，不能再静默吞掉
    assert "[audio] 播放失败" in js


def test_wake_ack_reports_real_container(monkeypatch):
    """唤醒应答的 format 必须按实际字节判断。

    这个 bug 真出现过：ACK 事件硬编码 format='mp3'，而本地 VITS 产出的是 WAV，
    前端据此用错 MIME（"有文字没声音"）。
    """
    import asyncio
    import wave
    from io import BytesIO

    import app.api.agent as agent_api
    from app.voice import tts_cache

    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    wav_bytes = buf.getvalue()

    monkeypatch.setattr(tts_cache, "get", lambda text, voice, rate=None: wav_bytes)
    out = asyncio.run(agent_api.wake_ack())
    assert out["ok"] is True
    assert out["format"] == "wav", "应答音频是 WAV，format 不能报 mp3"
