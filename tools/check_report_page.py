"""第三页 = 健康报告（不是底部按钮弹出的覆盖层）—— 浏览器行为回归。

验证：切到第三页时报告真的在设备屏里渲染、MJPEG 停流、底栏不再有报告按钮；
切回前两页能正常恢复（表情页 DOM / 摄像头页视频流）。
前置：服务已在 127.0.0.1:8765 运行。
"""
import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def state(page) -> dict:
    raw = page.evaluate("""() => (0,eval)('JSON.stringify({page: _page,'
      + ' face: !document.getElementById("faceLayer").hidden,'
      + ' report: !document.getElementById("reportPage").hidden,'
      + ' screen: !document.getElementById("screen").hidden,'
      + ' btn: !!document.getElementById("btnReport"),'
      + ' rpClose: !!document.getElementById("rpClose"),'
      + ' dots: [...document.querySelectorAll("#pageDots i")].map(i => i.classList.contains("on")),'
      + ' dev: (() => { const r = document.getElementById("device").getBoundingClientRect();'
      + '   return [Math.round(r.width), Math.round(r.height)]; })(),'
      + ' reportBox: (() => { const e = document.getElementById("reportPage");'
      + '   const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })(),'
      + ' bodyScroll: (() => { const b = document.getElementById("rpBody");'
      + '   return [Math.round(b.clientHeight), Math.round(b.scrollHeight)]; })(),'
      + ' report_txt: (document.getElementById("rpBody").innerText||"").slice(0,60),'
      + ' img_src: document.getElementById("screen").getAttribute("src")})')""")
    return json.loads(raw)


def check_device_geometry(label: str, s: dict) -> None:
    """设备框必须一直是 640:480 的样子（第三页曾被 position:absolute 搞塌到 28px）。"""
    w, h = s["dev"]
    want = w * 480 / 640
    check(f"{label}：设备框比例正常（{w}×{h}，期望高≈{want:.0f}）", abs(h - want) <= 8)


def goto(page, target: str) -> dict:
    page.evaluate("(p) => goPage(p)", target)
    page.wait_for_timeout(1500)
    return state(page)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel="msedge", args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1100, "height": 800})
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.__dshBooted && typeof goPage === 'function'")
    page.wait_for_timeout(2500)

    print("\n=== ① 表情页（第 1 页）===")
    s = goto(page, "face")
    check("表情层可见", s["face"] is True, str(s))
    check("摄像头流已停", s["screen"] is False and not s["img_src"], str(s.get("img_src")))
    check("报告页隐藏", s["report"] is False, str(s))
    check_device_geometry("表情页", s)

    print("\n=== ② 摄像头页（第 2 页）===")
    s = goto(page, "camera")
    check("摄像头流在播", s["screen"] is True and s["img_src"] == "/api/device/stream", str(s))
    check("表情层/报告页都隐藏", s["face"] is False and s["report"] is False, str(s))
    check_device_geometry("摄像头页", s)

    print("\n=== ③ 健康报告页（第 3 页）===")
    s = goto(page, "stats")
    check("报告页可见", s["report"] is True, str(s))
    check("摄像头流已停（省 CPU）", s["screen"] is False, str(s.get("img_src")))
    check("表情层隐藏", s["face"] is False, str(s))
    check("底栏没有报告按钮（不再放底部）", s["btn"] is False, str(s))
    check("没有多余的关闭按钮（整页就是这一页）", s["rpClose"] is False, str(s))
    check("页码点在第 3 个", s["dots"] == [False, False, True], str(s["dots"]))
    check("报告内容真的加载了", len(s["report_txt"]) > 5, repr(s["report_txt"]))
    check_device_geometry("报告页", s)
    rw, rh = s["reportBox"]
    check("报告页占满屏幕区域（不是塌成一条）", rw >= 300 and rh >= 300, f"{rw}×{rh}")
    cbh, sbh = s["bodyScroll"]
    check("报告内容在页内滚动", cbh >= 150 and sbh >= cbh, f"可视 {cbh} / 内容 {sbh}")

    print("\n=== ④ 报告页内切标签 + 左右滑动切页 ===")
    page.evaluate("() => rpLoad('analysis')")
    page.wait_for_timeout(2500)
    s2 = state(page)
    check("切到「完整健康分析」有内容", len(s2["report_txt"]) > 5, repr(s2["report_txt"]))
    page.evaluate("() => step(-1)")                               # 左滑 → 回第 2 页
    page.wait_for_function("() => _page === 'camera'")
    s = state(page)
    check("左滑回摄像头页", s["page"] == "camera" and s["screen"] is True and s["report"] is False, str(s))
    page.evaluate("() => step(-1)")                               # 再左滑 → 第 1 页
    page.wait_for_function("() => _page === 'face'")
    s = state(page)
    check("再左滑回表情页", s["face"] is True and s["report"] is False, str(s))
    print("\nJS 错误:", errs or "无")
    browser.close()

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("REPORT PAGE CHECKS PASSED")
