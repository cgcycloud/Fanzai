"""连续模式下，机器人回复播完之后是否会自动重新收音（诊断/回归用）。

与 check_wake_single_vs_continuous.py 的区别：**不伪造 /api/agent/wake/feed**，
走真实的 KWS 唤醒链路（假麦克风循环播放「你好饭崽」），只断言"一轮之后有没有
自动开第二轮录音"，并把中间状态每 200ms 打一行，卡在哪一步一眼能看出来。
"""
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
MIC = os.environ.get("MINDFUL_MIC") or os.path.join(
    os.environ.get("TEMP", r"C:\Windows\Temp"), "wake_browser_mic.wav")

_STATE_JS = """JSON.stringify({
  rec: !!_recording, wake: !!_wakeOn, dlg: !!_dlgBusy, play: !!_playing,
  q: _audioQueue.length, sess: !!_wakeSessionActive, wait: !!_waitingNextTurn,
  cont: !!wakeContinuousNow(), ack: !!_wakeAckPlaying, floor: Math.round(_wakeFloor*10)/10,
  status: (document.getElementById('runStatus')||{}).textContent||''
})"""
PROBE = f"() => (0,eval)({json.dumps(_STATE_JS)})"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    original = get("/api/agent/settings")["settings"]
    post("/api/agent/settings", {"wake_mode": "continuous", "autonomy": "off"})
    print(f"唤醒模式=continuous（原 {original['wake_mode']}），自主互动=off"
          f"（原 {original['autonomy']}），麦克风={MIC}")
    errs, lines = [], []
    result = {"episodes": 0}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True, args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={MIC}",
                "--autoplay-policy=no-user-gesture-required",
            ])
            page = browser.new_context(permissions=["microphone"]).new_page()
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.on("console", lambda m: errs.append("console:" + m.text)
                    if m.type == "error" else None)
            page.goto(BASE, wait_until="domcontentloaded")
            page.wait_for_function("() => typeof onWake === 'function'")
            page.wait_for_timeout(2500)
            post("/api/agent/wake/reset", {"session": "diag"})

            t0 = time.time()
            prev_rec = False
            first_turn_done_at = None
            deadline = t0 + 75
            while time.time() < deadline:
                s = json.loads(page.evaluate(PROBE))
                t = round(time.time() - t0, 2)
                lines.append((t, s))
                if s["rec"] and not prev_rec:
                    result["episodes"] += 1
                    print(f"[{t:6.2f}s] ▶ 第 {result['episodes']} 段收音开始"
                          f"（floor={s['floor']}）")
                prev_rec = s["rec"]
                if result["episodes"] >= 2:
                    first_turn_done_at = t
                    break
                page.wait_for_timeout(200)
            trace = " ".join(
                f"{t}:{'R' if s['rec'] else '-'}{'W' if s['wake'] else '-'}"
                f"{'D' if s['dlg'] else '-'}{'P' if s['play'] else '-'}"
                f"{'T' if s['wait'] else '-'}{'S' if s['sess'] else '-'}"
                for t, s in lines[::5])
            print("时间线 R=收音 W=待唤醒监听 D=对话中 P=播报 T=等下一句 S=会话中")
            print("  " + trace)
            print("最后状态:", json.dumps(lines[-1][1], ensure_ascii=False))
            page.screenshot(path="gui-test-screenshots/diag_continuous_resume.png")
            browser.close()
    finally:
        post("/api/agent/settings", {"wake_mode": original["wake_mode"],
                                     "autonomy": original["autonomy"]})
        post("/api/device/listen", {"active": False})
        print("已还原设置")

    print(f"收音段数={result['episodes']}（连续模式应 ≥2）")
    print("JS 错误:", errs or "无")
    return 0 if result["episodes"] >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
