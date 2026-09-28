"""实测设置面板的「单句 / 连续」切换是否生效（UI → 服务端 → 页面状态）。"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")


def server_mode():
    with urllib.request.urlopen(BASE + "/api/agent/settings", timeout=8) as r:
        return json.loads(r.read())["settings"]["wake_mode"]


def post_settings(patch: dict):
    req = urllib.request.Request(BASE + "/api/agent/settings",
                                 data=json.dumps(patch).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# 本脚本会**真的改用户设置**（模拟点面板按钮），所以先记下原值，跑完还原 ——
# 否则用户下次唤醒会发现自己被改成别的模式，还以为是"切换不生效"的 bug。
original_mode = server_mode()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True,
                                args=["--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    page.evaluate("openSheet()")
    page.wait_for_timeout(1200)

    info = page.evaluate("""() => {
      const box = document.getElementById('wakeSeg');
      return {
        hasSeg: !!box,
        buttons: box ? [...box.querySelectorAll('button')].map(b => ({
          key: b.dataset.key, text: b.textContent, on: b.classList.contains('on') })) : [],
        settingsMode: (typeof _settings !== 'undefined' && _settings) ? _settings.wake_mode : null,
      };
    }""")
    print("面板初始状态:", json.dumps(info, ensure_ascii=False))
    print("服务端初始:", server_mode())

    # 依次点「连续」「单句」，每步都核对 UI / 页面状态 / 服务端
    for want in ("continuous", "single"):
        label = "连续" if want == "continuous" else "单句"
        clicked = page.evaluate("""(want) => {
          const box = document.getElementById('wakeSeg');
          const btn = box ? [...box.querySelectorAll('button')].find(b => b.dataset.key === want) : null;
          if (!btn) return 'no-button';
          btn.click();
          return 'clicked';
        }""", want)
        page.wait_for_timeout(1800)
        after = page.evaluate("""() => ({
          onButtons: [...document.querySelectorAll('#wakeSeg button')]
                       .filter(b => b.classList.contains('on')).map(b => b.dataset.key),
          settingsMode: _settings ? _settings.wake_mode : null,
          modeChoice: typeof wakeModeChoice === 'function' ? wakeModeChoice() : null,
          desc: (document.getElementById('wakeDesc') || {}).textContent || '',
        })""")
        srv = server_mode()
        ok = (after["settingsMode"] == want and after["modeChoice"] == want and srv == want)
        print(f"\n点击「{label}」→ {clicked}")
        print(f"  面板高亮: {after['onButtons']} | 页面模式: {after['settingsMode']} / "
              f"wakeModeChoice={after['modeChoice']} | 服务端: {srv}")
        print(f"  说明文字: {after['desc']!r}")
        print(f"  {'✅ 生效' if ok else '❌ 没生效'}")

    print("\nJS 错误:", errs or "无")
    browser.close()

# 还原用户原来的唤醒模式
try:
    post_settings({"wake_mode": original_mode})
    print(f"\n已还原唤醒模式为原来的「{original_mode}」")
except Exception as exc:
    print(f"\n⚠ 还原唤醒模式失败（请手动到设置面板检查）：{exc}")
