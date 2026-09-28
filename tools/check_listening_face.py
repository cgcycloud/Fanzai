"""检查：每一段录音里，"输入中"表情出现没出现、隔多久出现、覆盖多久。

回归用 —— 用户反馈过"开了录音但小机器人还是待机脸，以为没在录"。
期望：每段录音都出现 input，且覆盖录音窗口的绝大部分时间。
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
MIC = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "wake_browser_mic.wav")

PROBE = """() => {
  const av = document.getElementById('avatar');
  return JSON.stringify({rec: !!_micOn, state: av ? String(av._state) : '',
                         wanted: av ? !!av._inputWanted : false,
                         dlg: !!_dlgBusy, play: !!_playing});
}"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={MIC}",
            "--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(permissions=["microphone"]).new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function("() => typeof onWake === 'function'")
        page.wait_for_timeout(2000)
        t0 = time.perf_counter()
        episodes, cur = [], None
        trace, traced = [], 0
        while time.perf_counter() - t0 < 70:
            s = json.loads(page.evaluate(PROBE))
            t = time.perf_counter() - t0
            if s["rec"] and traced < 2:
                trace.append((round(t, 2), s["state"], s["wanted"]))
            if s["rec"] and cur is None:
                cur = {"t0": t, "first_input": None, "input_ms": 0, "other": {}}
            if s["rec"] and cur is not None:
                if s["state"] == "input":
                    cur["input_ms"] += 60
                    if cur["first_input"] is None:
                        cur["first_input"] = t
                else:
                    cur["other"][s["state"]] = cur["other"].get(s["state"], 0) + 60
            if not s["rec"] and cur is not None:
                cur["t1"] = t
                episodes.append(cur)
                cur = None
                traced += 1
            page.wait_for_timeout(60)
        browser.close()

    for t, st, w in trace:
        print(f"   {t:6.2f}s  state={st:<8} inputWanted={w}")
    for i, e in enumerate(episodes, 1):
        dur = e["t1"] - e["t0"]
        shown = "—" if e["first_input"] is None else f"{e['first_input'] - e['t0']:.2f}s 后"
        print(f"录音段#{i}: 时长 {dur:5.2f}s  首次出现 input: {shown}  "
              f"input 占比 {e['input_ms']/1000:.2f}s  其它状态(ms): {e['other']}")
    print(f"共 {len(episodes)} 段；完全没出现过 input 的段数："
          f"{sum(1 for e in episodes if e['first_input'] is None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
