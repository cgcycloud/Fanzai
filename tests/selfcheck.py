"""一键自检（等价旧项目 run_selfcheck.py 的 8 步检查）。

用法: python tests/selfcheck.py [--skip 名称]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STEPS = []


def step(key, title):
    def deco(fn):
        STEPS.append((key, title, fn))
        return fn
    return deco


@step("imports", "核心模块导入")
def s_imports():
    from app.core.analytics import health_analyzer
    from app.core.ai_client import get_ai_client, LocalEchoClient
    from app.core.db import HealthStore
    from app.core.state_machine import DialogueStateMachine
    from app.server import app
    assert app is not None
    return "core + server 导入成功"


@step("db", "数据库读写")
def s_db():
    from app.core.db import HealthStore
    store = HealthStore()
    store.init()
    eid = store.add_event("selfcheck", {"ok": True}, status="local")
    assert eid > 0
    return f"事件写入 OK (id={eid})"


@step("state-machine", "对话状态机（16 状态/空消息/结束词）")
def s_sm():
    from app.core.state_machine import DialogueStateMachine
    sm = DialogueStateMachine()
    sm.start_full_meal_session()
    assert sm.state.name == "PRE_MEAL_BREATH"
    reply, fin = sm.process_user_input("")      # 空消息不推进
    assert not fin
    while sm.state.name != "END":
        sm.process_user_input("继续")
    assert sm.state.name == "END"
    sm2 = DialogueStateMachine(); sm2.start_full_meal_session()
    r, fin = sm2.process_user_input("谢谢，结束吧")
    assert fin and sm2.state.name == "END"
    return "状态机流转 + 空消息 + 结束词 全部通过"


@step("config-store", "AI 配置存储（单一可手改文件）")
def s_config_store():
    from app.core.ai_config import AIConfigStore
    with tempfile.TemporaryDirectory() as td:
        store = AIConfigStore(Path(td) / "c.json", Path(td) / "k.key")
        store.save("https://example.com/v4", "test-model", api_key="sk-test-12345")
        cfg = store.load()
        # Key 现在是明文（为了能直接手改 ai_config.json），不再写 encrypted_* 字段
        assert cfg.api_key == "sk-test-12345"
        assert cfg.key_set
        # 给接口/日志看的公开视图绝不外泄 Key
        assert "sk-test-12345" not in str(store.public_dict())
    return "配置落盘 + 回读 + 公开视图不泄露 Key OK"


@step("analytics", "健康分析（周报/风险/复查）")
def s_analytics():
    from app.core.analytics import health_analyzer
    r = health_analyzer("selfcheck")
    assert "week_report" in r and "personal_insight" in r
    return f"周报 {r['week_report']['meal_count']} 餐，依从性 {r['week_report']['adherence_score']} 分"


@step("vision", "视觉感知（情绪+咀嚼）")
def s_vision():
    import numpy as np
    from app.vision import PerceptionHub
    hub = PerceptionHub()
    frame = (np.zeros((240, 320, 3)) + 90).astype("uint8")
    snap = hub.analyze_frame(frame)
    assert "emotion" in snap and "chewing" in snap
    return f"情绪引擎={hub.emotion.engine_name} 咀嚼可用={hub.chewing.available}"


@step("asr", "离线语音识别（sherpa-onnx Paraformer）")
def s_asr():
    from app.voice.asr import active_engine, model_dir, warmup
    engine = active_engine()
    assert engine == "paraformer", (
        f"识别引擎不可用（{engine}）：模型缺失请运行 python -m tools.download_models（约定目录 {model_dir()}）")
    assert warmup(), "识别引擎预热失败"
    # 真实语音端到端：用 TTS 合成一句话，再识别回来比对
    import subprocess, tempfile, wave
    from pathlib import Path as _P
    from app.voice.asr import transcribe_wav
    ps = r"C:\Windows\System32\WindowsPowerShell/v1.0\powershell.exe"
    if _P(ps).exists():
        out = _P(tempfile.gettempdir()) / "mindful_selfcheck_speech.wav"
        subprocess.run([ps, "-NoProfile", "-Command",
                        "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=-1; "
                        f"$s.SetOutputToWaveFile('{out}'); $s.Speak('你好正念我有点饿'); $s.Dispose()"],
                       capture_output=True)
        if out.exists():
            from app.voice.convert import to_16k_wav
            conv = out.with_name("mindful_selfcheck_16k.wav")
            conv.write_bytes(to_16k_wav(out.read_bytes()))
            text, _ = transcribe_wav(conv)
            assert "饿" in text, f"识别结果异常: {text}"
            return f"引擎={engine}，真实语音识别通过：{text}"
    return f"引擎={engine}，模型加载 OK"


@step("wake", "语音唤醒（sherpa-onnx KWS 关键词模型）")
def s_wake():
    from app.voice.sherpa_kws import keyword_line, kws_detector
    assert kws_detector.available(), "唤醒模型缺失：请运行 python tools/download_sherpa_models.py"
    assert kws_detector.warmup(), f"KWS 加载失败: {kws_detector.load_error()}"
    line, missing = keyword_line("你好饭崽")
    assert line.endswith("@你好饭崽") and not missing, f"关键词编码异常: {line} {missing}"
    return f"KWS 就绪，关键词：{line}"


@step("tts", "TTS 合成（云端 Qwen3-TTS-Flash）")
def s_tts():
    import wave

    from app.voice.tts import active_engine, detect_format, synthesize
    engine = active_engine()
    assert engine != "unavailable", (
        "发声链路不可用：Qwen3-TTS 是云端合成，需要联网并配置 API Key —— "
        "请在网页设置页填入阿里云百炼的 TTS API Key（DASHSCOPE_API_KEY）")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "tts.bin"
        ok = synthesize("自检测试", out)
        assert ok and out.exists() and out.stat().st_size > 1000
        raw = out.read_bytes()
        fmt = detect_format(raw)
        if fmt == "wav":
            with wave.open(str(out), "rb") as w:
                detail = f" WAV {w.getframerate()}Hz"
        else:
            detail = " MP3"
    return f"当前引擎 {engine}（{detail.strip()}）合成 OK"


@step("server", "FastAPI 服务 + API 冒烟")
def s_server():
    from fastapi.testclient import TestClient
    from app.server import app
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/").status_code == 200
        r = client.post("/api/dialogue", json={"message": ""})
        assert r.status_code == 200 and r.json()["ok"]
        r = client.get("/api/config")
        assert r.status_code == 200 and "configured" in r.json()
        r = client.get("/api/analytics")
        assert r.status_code == 200 and "personal_insight" in r.json()
    return "健康检查/首页/对话/配置/分析 API 全通过"


@step("device-screen", "设备小屏渲染与页面")
def s_device():
    from app.config import SCREEN_H, SCREEN_W
    from app.display import display_state
    from app.display import render as R

    # 三个页面：高清横屏；详细页内容超出一屏（可滚动）
    for page in ("face", "camera", "stats"):
        display_state.set_page(page)
        img, content_h = R.render(display_state, t=1.0)
        assert img.size == (SCREEN_W, SCREEN_H), f"{page} 尺寸异常: {img.size}"
        assert (SCREEN_W, SCREEN_H) == (640, 480), "必须高清横屏 640x480"
        if page != "face":
            assert content_h > SCREEN_H, f"{page} 应可滚动: {content_h}"
    display_state.set_page("face")
    return f"3 页高清 OK（{SCREEN_W}x{SCREEN_H}，详细页可滚动）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", action="append", default=[])
    args = ap.parse_args()

    results = {}
    for key, title, fn in STEPS:
        if key in args.skip:
            results[key] = ("skip", "")
            continue
        try:
            note = fn()
            results[key] = ("ok", note)
            print(f"  ✅ {title}: {note}")
        except Exception as exc:
            results[key] = ("fail", f"{type(exc).__name__}: {exc}")
            print(f"  ❌ {title}: {type(exc).__name__}: {exc}")

    failed = [k for k, (st, _) in results.items() if st == "fail"]
    print("\n" + "=" * 50)
    if failed:
        print(f"❌ 自检未通过: {', '.join(failed)}")
        sys.exit(1)
    print(f"✅ 全部 {len(results)} 步自检通过")


if __name__ == "__main__":
    main()
