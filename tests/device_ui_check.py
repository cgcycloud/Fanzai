"""设备屏仿真界面验收：实况画面 + 左右滑动切页 + 语音对话。

用法: .venv/Scripts/python.exe tests/device_ui_check.py [语音wav]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"


def _page_scroll() -> float:
    import json as _json
    import urllib.request as _u
    try:
        d = _json.loads(_u.urlopen(BASE + "/api/device/state", timeout=5).read())
        return d.get("scroll", {}).get(d.get("page", ""), 0)
    except Exception:
        return -1


def _page_status(page) -> str:
    import json as _json
    import urllib.request as _u
    try:
        return _json.loads(_u.urlopen(BASE + "/api/device/status", timeout=5).read())["page"]
    except Exception:
        return "?"
OUT = Path("gui-test-screenshots")
OUT.mkdir(exist_ok=True)
SPEECH = sys.argv[1] if len(sys.argv) > 1 else None


def main() -> int:
    errors: list[str] = []
    args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
    if SPEECH:
        args.append(f"--use-file-for-fake-audio-capture={Path(SPEECH).resolve()}")
    args.append("--autoplay-policy=no-user-gesture-required")

    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True, args=args)
        ctx = br.new_context(viewport={"width": 1280, "height": 820}, permissions=["microphone"])
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"[console.error] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        # 表情页：组件已挂载且在渲染（SVG 内有内容）
        has_avatar = page.locator("#faceLayer agent-robot-avatar").count()
        svg_nodes = page.evaluate(
            "() => { const a = document.querySelector('#faceLayer agent-robot-avatar');"
            " return a && a.shadowRoot ? a.shadowRoot.querySelectorAll('svg *').length : 0; }")
        extra = page.locator("#side, #faceBtns, #pageBtns").count()
        print(f"[1] 表情页 = agent-robot-avatar 组件（挂载 {has_avatar}，SVG 元素 {svg_nodes}），"
              f"额外元素 {extra} 个（应为 0）")
        page.screenshot(path=str(OUT / "sim_1_face.png"))

        # 切到摄像头页，测 MJPEG 流帧率
        page.click("#pageDots i[data-page='camera']")
        page.wait_for_timeout(2500)
        fps = page.evaluate("""async () => {
          const r = await fetch('/api/device/stream');
          const reader = r.body.getReader();
          const chunks = [];
          const t0 = performance.now();
          while (performance.now() - t0 < 5000) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
          }
          reader.cancel().catch(() => {});
          let total = 0;
          for (const c of chunks) total += c.length;
          const all = new Uint8Array(total);
          let off = 0;
          for (const c of chunks) { all.set(c, off); off += c.length; }
          let n = 0;
          for (let i = 0; i + 2 < all.length; i++) {
            if (all[i] === 0xff && all[i + 1] === 0xd8 && all[i + 2] === 0xff) n++;
          }
          return n;
        }""")
        print(f"[1b] 摄像头页流帧率: 5 秒 {fps} 帧（≈{fps/5:.1f} fps）")
        page.click("#pageDots i[data-page='face']")
        page.wait_for_timeout(1200)

        # ---- 滑动切页：鼠标拖拽（模拟左右滑） ----
        box = page.locator("#device").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx + 120, cy)
        page.mouse.down()
        page.mouse.move(cx - 120, cy, steps=8)      # 向左滑 → 下一页
        page.mouse.up()
        page.wait_for_timeout(1200)
        print(f"[2] 向左滑动后 → 页面 {_page_status(page)}")

        page.mouse.move(cx + 120, cy)
        page.mouse.down()
        page.mouse.move(cx - 120, cy, steps=8)
        page.mouse.up()
        page.wait_for_timeout(1200)
        print(f"[3] 再滑一次 → 页面 {_page_status(page)}")

        # ---- 反向滑动回去 ----
        page.mouse.move(cx - 120, cy)
        page.mouse.down()
        page.mouse.move(cx + 120, cy, steps=8)
        page.mouse.up()
        page.wait_for_timeout(1000)
        print(f"[4] 向右滑动（返回）→ 页面 {_page_status(page)}")

        # ---- 纵向滚动（健康数据页内容 > 240 可滚）----
        page.mouse.move(cx + 140, cy); page.mouse.down()
        page.mouse.move(cx - 140, cy, steps=10); page.mouse.up()
        page.wait_for_timeout(600)          # → stats
        before = _page_scroll()
        page.mouse.move(cx, cy)
        page.mouse.down()
        for i in range(6):
            page.mouse.move(cx, cy - 20 * (i + 1), steps=3)   # 向上拖 = 内容向下滚
        page.mouse.up()
        page.wait_for_timeout(700)
        after = _page_scroll()
        print(f"[4.5] 健康数据页纵向拖动滚动: {before} → {after}（应增大）")
        page.screenshot(path=str(OUT / "sim_3b_scrolled.png"))

        # ---- 先回到 camera（用 API 定位，保证后续手势测试起点一致）----
        import json as _json
        import urllib.request as _u
        req = _u.Request(BASE + "/api/device/page",
                         _json.dumps({"page": "camera"}).encode(),
                         {"Content-Type": "application/json"})
        _u.urlopen(req, timeout=5)
        page.wait_for_timeout(600)

        # ---- 回到表情页，轻触说话 ----
        req = _u.Request(BASE + "/api/device/page",
                         _json.dumps({"page": "face"}).encode(),
                         {"Content-Type": "application/json"})
        _u.urlopen(req, timeout=5)
        page.wait_for_timeout(2600)      # 等 JS 低频同步 _page
        print(f"[6] 回到 {_page_status(page)}")
        if SPEECH:
            page.mouse.click(cx, cy)                     # 轻触 → 开始说话
            page.wait_for_timeout(20000)
            page.screenshot(path=str(OUT / "sim_6_voice.png"))
            turns = page.evaluate("async () => (await (await fetch('/api/device/state')).json()).turns.length")
            print(f"    轻触说话完成，设备屏对话轮次: {turns}")
        else:
            page.evaluate("""async () => {
              await fetch('/api/dialogue/stream', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ message: '我有点饿了' }),
              });
            }""")
            page.wait_for_timeout(9000)
            turns = page.evaluate("async () => (await (await fetch('/api/device/state')).json()).turns.length")
            print(f"    对话后设备屏对话轮次: {turns}")
        br.close()

    print("\n=== 控制台错误 ===")
    print("  无" if not errors else "\n".join("  " + e for e in errors[:10]))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
