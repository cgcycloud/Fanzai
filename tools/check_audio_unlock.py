"""确定性验证"自动播放被拦"的兜底逻辑。

真实浏览器是否拦截取决于时序（页面在采集麦克风时会放行），因此这里用注入脚本
**强制第一次 play() 抛 NotAllowedError**，然后断言：
  1) 这段音频没有丢 —— 被放回队列等待
  2) 状态栏给出"点一下屏幕开启声音"的提示
  3) 用户点击后解锁，并把这端音频补播出来
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"

# 拦截前 N 次 play()，模拟浏览器的自动播放限制
HOOK = """
window.__audioLog = [];
(function () {
  const origPlay = HTMLMediaElement.prototype.play;
  let blocked = 0;
  HTMLMediaElement.prototype.play = function () {
    const rec = { at: Math.round(performance.now()) };
    window.__audioLog.push(rec);
    this.addEventListener('playing', () => { rec.playing = true; });
    if (blocked < 1) {                    // 只拦第一次，之后放行
      blocked += 1;
      rec.simulatedBlock = true;
      const err = new DOMException('play() failed because the user didn\\'t interact',
                                   'NotAllowedError');
      return Promise.reject(err);
    }
    const p = origPlay.apply(this, arguments);
    if (p && p.then) p.catch(e => { rec.playRejected = e.name; });
    return p;
  };
})();
"""

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True,
                                args=["--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    page.add_init_script(HOOK)
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # 直接从页面内部走真实播放路径（拿 /api/tts 的真音频）
    state = page.evaluate("""async () => {
      const r = await fetch('/api/tts', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text:'测试声音'})});
      const d = await r.json();
      playB64(d.audio_b64, d.format);      // 这一发会被模拟拦下
      await new Promise(res => setTimeout(res, 800));
      return { blocked: _audioBlocked, queued: _audioQueue.length, playing: _playing,
               hint: document.getElementById('runStatus').textContent,
               unlocked: _audioUnlocked,
               log: window.__audioLog.map(x => ({blocked: !!x.simulatedBlock, playing: !!x.playing})) };
    }""")
    print("① 被拦后：", state)
    ok1 = state["blocked"] is True and state["queued"] >= 1 and "点一下屏幕" in state["hint"]
    print("   音频没丢（在队列里）:", state["queued"] >= 1, "| 有提示:", "点一下屏幕" in state["hint"])

    page.mouse.click(450, 300)            # 用户点一下
    page.wait_for_timeout(2500)
    after = page.evaluate("""() => ({
      unlocked: _audioUnlocked, queued: _audioQueue.length, playing: _playing,
      log: window.__audioLog.map(x => ({blocked: !!x.simulatedBlock, playing: !!x.playing})),
    })""")
    print("② 点击后：", after)
    # 判据：已解锁 + 被拦那一段被重新播放（队列清空、且第二次播放确实 playing）
    ok2 = (after["unlocked"] is True and after["queued"] == 0
           and any(x["playing"] and not x["blocked"] for x in after["log"]))

    browser.close()

ok = ok1 and ok2
print(f"\n被拦时保住音频并提示: {ok1} | 点击后解锁并补播: {ok2}")
print("=== 播放解锁链路:", "通过" if ok else "失败", "===")
sys.exit(0 if ok else 1)
