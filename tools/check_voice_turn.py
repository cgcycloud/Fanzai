"""全链路端到端：说唤醒词 → 应答「我在」→ 自动收音 → 识别 → AI 回复。

用假麦克风循环播放「你好饭崽」，设备页应当自动完成一整轮对话。
这条用例同时覆盖本轮改动过的静音断句阈值与"说话时长不足判为噪声"逻辑：
若阈值过严，语音会被丢弃，测试就会在"没有产生回复"上失败。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"
MIC = r"C:\Users\hp\AppData\Local\Temp\wake_browser_mic.wav"
OUT = Path(__file__).resolve().parents[1] / "gui-test-screenshots"


def wake_status():
    with urllib.request.urlopen(f"{BASE}/api/agent/wake", timeout=5) as r:
        return json.loads(r.read())


def get_settings():
    with urllib.request.urlopen(f"{BASE}/api/agent/settings", timeout=5) as r:
        return json.loads(r.read())


def post_settings(patch: dict):
    req = urllib.request.Request(f"{BASE}/api/agent/settings",
                                 data=json.dumps(patch).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


errors: list[str] = []
before = wake_status()["detector"]["hits"]
if not wake_status().get("enabled"):
    print("⚠ 语音唤醒在设置里处于【关闭】状态（设置面板 → 语音唤醒开关）。")
    print("  全链路测试需要唤醒可用，请先打开它。")
    sys.exit(2)
prev_autonomy = get_settings()["settings"]["autonomy"]
# 测试期间关掉自主互动：它会自己开口说话，而播报期间唤醒监听按设计被抑制
# （防"自己打断自己"），留着会干扰本用例的时序判断。
post_settings({"autonomy": "off"})
print(f"开始前 hits = {before}（自主互动 {prev_autonomy} → off）")

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--use-fake-ui-for-media-stream",
                  "--use-fake-device-for-media-stream",       # 必须有：否则用的是真实麦克风
                  f"--use-file-for-fake-audio-capture={MIC}",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        ctx = browser.new_context(permissions=["microphone"])
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        asr_calls, dlg_calls = [], []
        page.on("request", lambda r: asr_calls.append(r.url) if "/api/asr" in r.url else None)
        page.on("request", lambda r: dlg_calls.append(r.url)
                if "/api/dialogue/stream" in r.url else None)

        page.goto(BASE, wait_until="domcontentloaded")

        woke = recorded = False
        deadline = time.time() + 90
        while time.time() < deadline:
            st = page.evaluate("""() => ({
              status: (document.getElementById('runStatus') || {}).textContent || '',
              dlg: (document.getElementById('dlgBox') || {}).textContent || '',
              recording: typeof _recording !== 'undefined' ? _recording : null,
            })""")
            if "已唤醒" in st["status"]:
                woke = True
            if st["recording"]:
                recorded = True
            # 判据：真的发生了 识别 → 对话 两跳，且对话区出现用户轮次
            if woke and asr_calls and dlg_calls and "你 " in st["dlg"]:
                break
            page.wait_for_timeout(1000)

        page.screenshot(path=str(OUT / "verify_voice_turn.png"))
        final = page.evaluate("""() => ({
          status: (document.getElementById('runStatus') || {}).textContent || '',
          dlg: (document.getElementById('dlgBox') || {}).textContent || '',
        })""")
        ctx.close()
        browser.close()
finally:
    post_settings({"autonomy": prev_autonomy})          # 恢复用户原来的设置

hits_after = wake_status()["detector"]["hits"]
print(f"结束后 hits = {hits_after}")
print("状态栏:", final["status"][:60])
print("对话区:", final["dlg"][:160].replace("\n", " | "))
print(f"唤醒={woke} 开始收音={recorded} ASR请求={len(asr_calls)} 对话请求={len(dlg_calls)}")
print("控制台错误:", errors if errors else "无")

ok = (hits_after > before and woke and recorded and asr_calls and dlg_calls
      and "你 " in final["dlg"] and not errors)
print("=== 全链路:", "通过" if ok else "失败", "===")
sys.exit(0 if ok else 1)
