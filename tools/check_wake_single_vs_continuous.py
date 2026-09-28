"""实测「单句 / 连续」两种唤醒模式**行为**是否真的不同（不是只看设置存没存上）。

为什么需要这个脚本：`tools/check_wake_mode_switch.py` 只验证"点了按钮 → 设置存到服务端"，
但用户说的是**行为**没变化 —— 单句模式下说完一句还在听、连续模式下说完一句却停了。
这里把麦克风/识别/对话全部换成确定性的假数据，走真实前端代码，直接观察：

    单句：唤醒 → 应答 → 收音 → 一轮回答 → **回到待唤醒**（不再开麦）
    连续：唤醒 → 应答 → 收音 → 一轮回答 → **自动继续收音**（不回到待唤醒）

前置：服务已在 127.0.0.1:8765 运行（脚本会临时改唤醒模式，跑完还原）。
"""
import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
WAKE_HIT = {"matched": True, "word": "你好饭崽", "text": "你好饭崽", "confidence": 1.0}
WAKE_MISS = {"matched": False, "text": "", "confidence": 0.0}

# 唤醒只"命中"一次：之后 feed 一律不命中，等价于用户不再喊唤醒词。
# 否则单句模式回到待唤醒后会被喂进来的假唤醒词又拉进录音，测出来的行为是假的。
WAKE_ARMED = {"on": False}


def post(path, payload=None, raw=None, headers=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body,
                                 headers=headers or {"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read() or b"{}")


SSE_DIALOGUE = "".join([
    'event: reply\ndata: {"text":"好的，慢慢吃，感受这一口的味道。"}\n\n',
    'event: done\ndata: {"phase":"pre_meal","finished":false,'
    '"emotion":"neutral","full_reply":"好的，慢慢吃，感受这一口的味道。"}\n\n',
])

# device_ui.js 是普通脚本，顶层 let 不能靠 Playwright 的表达式注入读到（会 ReferenceError），
# 用间接 eval 在页面全局作用域里取状态最稳。
_STATE_JS = ("JSON.stringify({recording: !!_recording, wakeOn: !!_wakeOn,"
             " dlgBusy: !!_dlgBusy, session: !!_wakeSessionActive,"
             " continuous: !!wakeContinuousNow(), mode: wakeModeChoice()})")
PROBE = f"() => (0,eval)({json.dumps(_STATE_JS)})"


def probe(page) -> dict:
    return json.loads(page.evaluate(PROBE))


def timeline(page, seconds: float, interval: float = 0.25):
    """按固定间隔采样前端状态，得到这一轮的行为时间线。"""
    out, end = [], time.time() + seconds
    while time.time() < end:
        out.append((round(time.time() - (end - seconds), 2), probe(page)))
        page.wait_for_timeout(int(interval * 1000))
    return out


def run_mode(page, mode: str) -> dict:
    post("/api/agent/settings", {"wake_mode": mode})
    page.goto(BASE, wait_until="domcontentloaded")     # 重新加载，让页面读到新模式
    page.wait_for_function("() => !!window.__dshBooted && typeof onWake === 'function'")
    page.wait_for_timeout(2000)                       # 等 init/loadSettings 完成
    post("/api/agent/wake/reset", {"session": "harness", "all": True})

    before = probe(page)
    WAKE_ARMED["on"] = False                           # 下面直接模拟命中
    page.evaluate("() => onWake({word: '你好饭崽'})")   # 直接模拟命中唤醒词
    line = timeline(page, 16.0)

    # 数"收音段"：唤醒后的第一轮 = 第 1 段；连续模式应当自己再开第 2 段
    episodes, prev = 0, False
    for _, s in line:
        if s["recording"] and not prev:
            episodes += 1
        prev = s["recording"]
    final = line[-1][1]
    return {"mode": mode, "settings_mode": before["mode"], "wake_listening_before": line[0][1]["wakeOn"],
            "took_a_turn": episodes >= 1, "recording_episodes": episodes,
            "auto_reopens_mic": episodes >= 2,
            "back_to_wake": bool(final["wakeOn"] and not final["recording"]),
            "final": final, "timeline": line}


original = get("/api/agent/settings")["settings"]["wake_mode"]
results, errs = {}, []
try:
  with sync_playwright() as p:
    # 只装了 Edge（没有 Playwright 自带的 Chromium），与其它工具保持一致
    browser = p.chromium.launch(headless=True, channel="msedge", args=[
        "--autoplay-policy=no-user-gesture-required",
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--no-sandbox",
    ])
    page = browser.new_page()
    page.on("pageerror", lambda e: errs.append(str(e)))

    # 把"云端依赖"全部换成确定性假数据：本脚本只验证唤醒模式的行为接线
    page.route("**/api/agent/wake/ack", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"ok":false,"text":"我在。"}'))
    page.route("**/api/agent/wake/feed", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(WAKE_HIT if WAKE_ARMED["on"] else WAKE_MISS)))
    page.route("**/api/asr", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"ok": True, "text": "红烧肉挺好吃的"})))
    page.route("**/api/dialogue/stream", lambda r: r.fulfill(
        status=200, content_type="text/event-stream", body=SSE_DIALOGUE))
    page.route("**/api/agent/proactive", lambda r: r.fulfill(
        status=200, content_type="text/event-stream", body=""))

    for mode in ("single", "continuous"):
        print(f"\n=== 唤醒模式：{mode} ===")
        res = run_mode(page, mode)
        results[mode] = res
        print(f"  页面读到的设置：{res['settings_mode']}（应等于 {mode}）")
        print(f"  待唤醒监听中：{res['wake_listening_before']} / 真的收了一轮音：{res['took_a_turn']}")
        print(f"  收音段数：{res['recording_episodes']}"
              f"（单句应为 1，连续应 ≥2）")
        print(f"  一轮之后自动接着听：{res['auto_reopens_mic']} / 最后停在待唤醒：{res['back_to_wake']}")
        trace = " ".join(f"{t}s:{'R' if s['recording'] else '-'}{'W' if s['wakeOn'] else '-'}"
                         f"{'D' if s['dlgBusy'] else '-'}" for t, s in res["timeline"])
        print(f"  时间线(R=收音 W=待唤醒 D=对话中)：{trace}")

    browser.close()
finally:
    # 中途报错也要把唤醒模式和忙碌标志还原 —— 否则用户下次唤醒会发现模式被改了，
    # 还以为是"切换不生效"的 bug（这个脚本真的踩过一次）。
    try:
        post("/api/agent/settings", {"wake_mode": original})
        post("/api/device/listen", {"active": False})
        print(f"\n已还原唤醒模式为「{original}」，并解除设备忙碌标志")
    except Exception as exc:
        print(f"\n⚠ 还原失败，请手动检查设置面板：{exc}")

ok_single = (results.get("single", {}).get("took_a_turn")
             and results["single"]["back_to_wake"]
             and not results["single"]["auto_reopens_mic"])
ok_cont = (results.get("continuous", {}).get("took_a_turn")
           and results["continuous"]["auto_reopens_mic"]
           and not results["continuous"]["back_to_wake"])
print()
print(f"单句模式：{'✅ 说完一句就回待唤醒' if ok_single else '❌ 行为不对'}")
print(f"连续模式：{'✅ 说完一句继续听' if ok_cont else '❌ 行为不对'}")
print("JS 错误:", errs or "无")

sys.exit(0 if (ok_single and ok_cont) else 1)
