"""真实浏览器 GUI 验收（用系统 Edge，无需下载浏览器）。

用法: .venv/Scripts/python.exe tests/gui_check.py
产出: gui-test-screenshots/*.png + 控制台错误清单
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/"   # 正式前端（设备屏；管理后台已删除）
OUT = Path("gui-test-screenshots")
OUT.mkdir(exist_ok=True)


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  permissions=[])
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type in ("error",) else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        # ---------- 页1：对话 + 摄像头 ----------
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)      # 等初始化 + 摄像头首帧
        page.screenshot(path=str(OUT / "t1_dialogue.png"))
        print("[1] 对话页截图完成")
        print("    首条 AI 消息:", (page.locator(".msg.ai").first.inner_text()[:40]
                                   if page.locator(".msg.ai").count() else "（无）"))
        print("    表情按钮数:", page.locator(".emoji-btn").count())
        print("    卡片数:", page.locator("#camCards .citem").count())
        print("    摄像头状态:", page.locator("#camOverlay").inner_text())
        print("    卡片首项:", page.locator("#camCards .citem").first.inner_text().replace("\n", " "))

        # ---------- 发送一条消息（验证流式气泡） ----------
        page.fill("#dlgInput", "我有点饿了")
        page.click("#btnSend")
        page.wait_for_timeout(7000)
        bubbles = page.locator(".msg.ai").all_inner_texts()
        print("    发送后 AI 气泡数:", len(bubbles))
        print("    最新回复:", bubbles[-1][:60] if bubbles else "（无）")
        page.screenshot(path=str(OUT / "t2_streaming.png"))

        # ---------- 页2：健康看板 ----------
        page.click("#nav-1")
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "t3_board.png"))
        print("[2] 看板页截图完成")
        print("    依从性评分:", page.locator("#aScore").inner_text(),
              "| 用餐次数:", page.locator("#aMeal").inner_text())
        print("    个性化洞察:", page.locator("#insightText").inner_text()[:60])
        page.click("text=生成")     # 生成个性文档
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "t4_personal_doc.png"))
        doc = page.locator("#personalDoc").inner_text()
        print("    个性文档长度:", len(doc), "| 开头:", doc[:40].replace("\n", " "))

        # ---------- 页3：设备屏幕镜像 ----------
        page.click("#nav-2")
        page.wait_for_timeout(3500)
        page.screenshot(path=str(OUT / "t7_device.png"))
        print("[3] 设备屏页截图完成")
        print("    镜像图尺寸:", page.evaluate(
            "() => { const i = document.getElementById('screenImg'); return i.naturalWidth + 'x' + i.naturalHeight; }"))
        print("    状态:", page.locator("#screenStatus").inner_text())
        page.click("text=② 摄像头")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "t8_device_camera.png"))
        print("    切换后:", page.locator("#screenStatus").inner_text())
        page.click("text=① 表情主页")
        page.wait_for_timeout(800)

        # ---------- 页4：设置 ----------
        page.click("#nav-3")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "t5_settings.png"))
        print("[4] 设置页截图完成")
        print("    AI 配置状态:", page.locator("#cfgState").inner_text())
        print("    API 地址框:", page.locator("#cfgUrl").input_value() or "（空）")
        page.click("text=查看热量库")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "t6_food_db.png"))
        print("    热量库条目:", page.locator("#foodResult .citem").count())

        # ---------- 麦克风按钮存在性（无麦克风环境不点） ----------
        page.click("#nav-0")
        page.wait_for_timeout(800)
        print("    麦克风按钮:", page.locator("#btnMic").inner_text(), "可见:", page.locator("#btnMic").is_visible())

        browser.close()

    print("\n=== 控制台错误 ===")
    if errors:
        for e in errors[:15]:
            print(" ", e)
    else:
        print("  无")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
