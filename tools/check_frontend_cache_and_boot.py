"""验证缓存策略 + 流式响应未被破坏 + 脚本加载失败时的兜底提示。"""
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"


def headers(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return {k.lower(): v for k, v in r.headers.items()}


print("=== 1) 缓存头 ===")
for p in ("/", "/static/js/device_ui.js?v=20260915b",
          "/static/css/device_ui.css?v=20260915b",
          "/static/vendor/agent-robot-avatar/agent-robot-avatar.js"):
    h = headers(p)
    print(f"  {p:56s} → {h.get('cache-control', '(可缓存)')}")

print("\n=== 2) 流式响应仍然正常（SSE 对话）===")
req = urllib.request.Request(BASE + "/api/dialogue/stream",
                             data=json.dumps({"message": "你好"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
t0 = time.time()
first_at = None
events = 0
with urllib.request.urlopen(req, timeout=90) as resp:
    buf = ""
    for line in resp:
        buf += line.decode("utf-8", "replace")
        if not line.strip() and buf.strip():
            events += 1
            if first_at is None:
                first_at = time.time() - t0
            buf = ""
print(f"  首事件 {first_at * 1000 if first_at else -1:.0f}ms（流式应远小于整体耗时）"
      f" | 事件数 {events}")

print("\n=== 3) 视频流（MJPEG）仍可读 ===")
try:
    req = urllib.request.Request(BASE + "/api/device/stream")
    with urllib.request.urlopen(req, timeout=10) as r:
        chunk = r.read(4096)
    print(f"  读到 {len(chunk)} 字节，含分片标记: {b'--frame' in chunk}")
except Exception as exc:
    print("  失败:", exc)

print("\n=== 4) 正常加载设备页 ===")
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True,
                         args=["--autoplay-policy=no-user-gesture-required"])
    page = b.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    st = page.evaluate("""() => ({
      booted: !!window.__dshBooted,
      loadingText: (document.getElementById('loading') || {}).textContent || '',
      hidden: getComputedStyle(document.getElementById('loading')).display === 'none',
      status: (document.getElementById('runStatus') || {}).textContent || '',
    })""")
    print("  脚本已启动:", st["booted"], "| 遮罩已隐藏:", st["hidden"])
    print("  状态栏:", st["status"])
    print("  JS 错误:", errs or "无")

    print("\n=== 5) 脚本加载失败时的兜底提示（拦截 JS 请求模拟）===")
    page2 = b.new_page()
    page2.route("**/static/js/device_ui.js*", lambda route: route.abort())
    page2.goto(BASE, wait_until="domcontentloaded")
    page2.wait_for_timeout(7500)          # 兜底定时器在 6 秒
    st2 = page2.evaluate("""() => ({
      text: (document.getElementById('loading') || {}).textContent || '',
      hidden: getComputedStyle(document.getElementById('loading')).display === 'none',
    })""")
    print("  遮罩可见:", not st2["hidden"], "| 文案:", st2["text"][:70].replace("\n", " "))
    ok_fallback = (not st2["hidden"]) and ("Ctrl+F5" in st2["text"])
    print("  兜底提示生效:", ok_fallback)
    b.close()
