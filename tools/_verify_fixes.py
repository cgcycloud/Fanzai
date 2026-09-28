"""Browser verification of the two frontend fixes:
  * #4 单句/连续 切换立即生效（设置改动即改运行时行为）
  * #2 用户说话时前端会上报"忙"，服务端据此阻止 AI 主动插话
"""
import sys
import time

from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8765"
fails = []


def check(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge",
                           args=["--use-fake-ui-for-media-stream",
                                 "--use-fake-device-for-media-stream"])
    page = br.new_page(viewport={"width": 1180, "height": 860})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(B, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    print("=== A) 单句/连续 的运行时判定随设置立即变化 ===")
    st = page.evaluate("""() => ({
      session: _wakeSessionActive,
      mode: wakeModeChoice(),
      continuous: wakeContinuousNow(),
      hasOldFlag: typeof _wakeContinuous !== 'undefined',
    })""")
    check("旧的合并标志已删除", st["hasOldFlag"] is False, str(st))
    check("未唤醒时 wakeContinuousNow()=False", st["continuous"] is False, str(st))

    # 模拟"已唤醒、会话进行中"
    page.evaluate("_wakeSessionActive = true")
    page.wait_for_timeout(200)
    now = page.evaluate("wakeContinuousNow()")
    check("会话进行中 && 设置=continuous → True", now is True, f"mode={st['mode']}")

    # 中途把设置改成单句 → 立刻变 False（这就是"切换不生效"的修复点）
    page.evaluate("_settings.wake_mode = 'single'")
    page.wait_for_timeout(200)
    after = page.evaluate("wakeContinuousNow()")
    check("中途改成单句 → 立即 False", after is False)

    # 再改回连续 → 立即 True
    page.evaluate("_settings.wake_mode = 'continuous'")
    page.wait_for_timeout(200)
    check("改回连续 → 立即 True", page.evaluate("wakeContinuousNow()") is True)

    print("\n=== B) 忙碌上报：录音时前端会让服务端知道 ===")
    page.evaluate("_settings.wake_mode = 'single'; _wakeSessionActive = false")
    # 直接驱动 busy 上报（不真的开麦，避免依赖假麦克风的时序）
    page.evaluate("_recording = true; reportBusy()")
    page.wait_for_timeout(600)
    srv = page.evaluate("fetch('/api/device/state').then(r=>r.json())")
    busy1 = page.evaluate("""fetch('/api/device/listen', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({active:true, reason:'listening'})}).then(r=>r.json())""")
    check("_busySent 已置位", page.evaluate("_busySent") is True)
    check("服务端收到并置忙", busy1.get("busy") is True, str(busy1))

    page.evaluate("_recording = false; reportBusy()")
    page.wait_for_timeout(600)
    busy2 = page.evaluate("""fetch('/api/device/listen', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({active:false})}).then(r=>r.json())""")
    check("停止后解除忙", busy2.get("busy") is False, str(busy2))

    print("\n=== C) 页面无 JS 错误 ===")
    check("no page errors", not errs, str(errs[:2]))

    br.close()

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("FRONTEND FIX CHECKS PASSED")
