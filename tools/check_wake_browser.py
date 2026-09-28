# 浏览器端到端：假麦克风循环播放「你好饭崽」→ 设备页应自动唤醒并进入聆听
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
OUT.mkdir(exist_ok=True)


def wake_status():
    with urllib.request.urlopen(f"{BASE}/api/agent/wake", timeout=5) as r:
        return json.loads(r.read())


errors: list[str] = []
before = wake_status()["detector"]["hits"]
st0 = wake_status()
if not st0.get("enabled"):
    print("⚠ 语音唤醒在设置里处于【关闭】状态（设置面板 → 语音唤醒开关）。")
    print("  这会让唤醒测试必然失败 —— 请先在设备屏下拉设置面板里打开它。")
    sys.exit(2)
print(f"唤醒前 detector hits = {before}（引擎 {st0.get('engine')}）")

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="msedge",
        headless=True,
        args=[
            "--use-fake-ui-for-media-stream",          # 自动授权麦克风
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={MIC}",  # 用我们的 wav 当麦克风
            "--autoplay-policy=no-user-gesture-required",
        ],
    )
    ctx = browser.new_context(viewport={"width": 900, "height": 700},
                              permissions=["microphone"])
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # 设置面板应显示唤醒开关与模式
    sheet_ok = page.evaluate("""() => {
      const w = document.getElementById('setWake');
      const seg = document.getElementById('wakeSeg');
      const words = document.getElementById('wakeWords');
      return { exists: !!w, checked: w ? w.checked : null,
               segButtons: seg ? seg.querySelectorAll('button').length : 0,
               words: words ? words.textContent : '' };
    }""")
    print("设置面板:", sheet_ok)

    # 等唤醒（假麦克风循环播放），并记录 应答播放/开始收音 的时序
    woke = False
    seen_status = ""
    saw_ack = saw_recording = False
    ack_fmt = None
    deadline = time.time() + 40
    while time.time() < deadline:
        state = page.evaluate("""() => ({
          status: (document.getElementById('runStatus') || {}).textContent || '',
          dlg: (document.getElementById('dlgBox') || {}).textContent || '',
          ackFetched: typeof _wakeAckB64 !== 'undefined' ? !!_wakeAckB64 : null,
          ackFmt: typeof _wakeAckFmt !== 'undefined' ? _wakeAckFmt : null,
          ackPlaying: typeof _wakeAckPlaying !== 'undefined' ? _wakeAckPlaying : null,
          recording: typeof _recording !== 'undefined' ? _recording : null,
        })""")
        seen_status = state["status"] or seen_status
        if state["ackFetched"]:
            saw_ack = True
            ack_fmt = state["ackFmt"]
        if state["recording"]:
            saw_recording = True
        if "已唤醒" in state["status"] or "🔔" in state["dlg"]:
            woke = True
        if woke and saw_ack and saw_recording:
            break
        page.wait_for_timeout(300)

    page.screenshot(path=str(OUT / "verify_wake_word.png"))
    hits_after = wake_status()["detector"]["hits"]
    print(f"唤醒后 detector hits = {hits_after}")
    print("状态栏:", seen_status)
    print("唤醒提示:", woke, "| 已取到应答语音:", saw_ack, "| 已开始收音:", saw_recording)
    print("应答音频容器:", ack_fmt, "（云端晓晓=mp3、本地音色=wav —— 前端按 format 选 MIME）")
    print("命中词:", wake_status()["detector"].get("last_word"))
    ctx.close()
    browser.close()

print("控制台错误:", errors if errors else "无")
ok = (woke and hits_after > before and sheet_ok["exists"] and sheet_ok["segButtons"] == 2
      and saw_ack and saw_recording)
print("=== 结论:", "通过" if ok else "失败", "===")
sys.exit(0 if ok else 1)
