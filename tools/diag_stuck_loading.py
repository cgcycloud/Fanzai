"""复现"卡在 正在连接设备屏…"：加载设备页并抓取所有 JS 错误与遮罩状态。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True,
                                args=["--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    errors, console = [], []
    page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: errors.append(f"{type(e).__name__}: {e}"))
    page.on("requestfailed", lambda r: errors.append(
        f"请求失败 {r.url} ({r.failure})"))

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)

    state = page.evaluate("""() => {
      const l = document.getElementById('loading');
      return {
        loading: l ? {hidden: l.hidden, classes: l.className,
                      display: getComputedStyle(l).display} : null,
        avatarDefined: !!(window.customElements && customElements.get('agent-robot-avatar')),
        hasInit: typeof init === 'function',
        hasRecording: typeof _recording !== 'undefined',
        hasPlayNext: typeof playNext === 'function',
        status: (document.getElementById('runStatus') || {}).textContent || '',
      };
    }""")
    print("页面状态:", state)
    print("\nJS 错误:")
    for e in errors or ["（无）"]:
        print("   ", e)
    print("\n控制台输出（最后 12 条）:")
    for line in console[-12:] or ["（无）"]:
        print("   ", line)
    browser.close()
