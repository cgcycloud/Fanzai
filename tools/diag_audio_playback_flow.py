"""在真实流程里追踪音频：钩住 HTMLMediaElement.play，看每一步有没有真的出声。

用于定位"有文字反馈没有声音"——区分：
  a) 根本没调用 play（前端没收到/没播放）
  b) 调用了 play 但被拒绝（自动播放策略/用户未交互）
  c) 播放出错（解码失败，例如 MIME/容器不对）
  d) 正常播放
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"
MIC = r"C:\Users\hp\AppData\Local\Temp\wake_browser_mic.wav"

HOOK = """
window.__audioLog = [];
(function () {
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    const rec = { at: Math.round(performance.now()), type: this.tagName,
                  src: String(this.src || '').slice(0, 12), volume: this.volume };
    window.__audioLog.push(rec);
    this.addEventListener('playing', () => { rec.playing = true; });
    this.addEventListener('error', () => {
      rec.errorCode = this.error ? this.error.code : -1;
      rec.errorMsg = this.error && this.error.message ? this.error.message : '';
    });
    const p = origPlay.apply(this, arguments);
    if (p && p.then) p.then(() => { rec.playResolved = true; })
                     .catch(e => { rec.playRejected = e.name + ': ' + e.message; });
    return p;
  };
})();
"""


def node(path, payload=None):
    if payload is None:
        with urllib.request.urlopen(BASE + path, timeout=8) as r:
            return json.loads(r.read())
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


before = node("/api/agent/wake")["detector"]["hits"]
print(f"开始前 hits={before}")

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="msedge", headless=True,
        # 故意不加 --autoplay-policy，模拟用户真实浏览器（未交互时会被拦）
        args=["--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-audio-capture={MIC}"],
    )
    ctx = browser.new_context(permissions=["microphone"])
    page = ctx.new_page()
    page.add_init_script(HOOK)
    logs = []
    page.on("console", lambda m: logs.append(m.text))
    page.goto(BASE, wait_until="domcontentloaded")

    for i in range(9):
        page.wait_for_timeout(4000)
        st = page.evaluate("""() => ({
          status: (document.getElementById('runStatus') || {}).textContent || '',
          dlg: (document.getElementById('dlgBox') || {}).textContent || '',
          log: window.__audioLog || [],
          playing: typeof _playing !== 'undefined' ? _playing : null,
          queue: typeof _audioQueue !== 'undefined' ? _audioQueue.length : null,
        })""")
        print(f"\nt={4 * (i + 1)}s 播放中={st['playing']} 队列={st['queue']} 状态={st['status'][:34]!r}")
        for r in st["log"]:
            state = ("playing✅" if r.get("playing") else
                     "rejected❌ " + str(r.get("playRejected")) if r.get("playRejected") else
                     "error❌ code=" + str(r.get("errorCode")) if r.get("errorCode") else "pending…")
            print(f"    [{r['at']:6d}ms] {r['type']} vol={r['volume']} → {state}")
        if st["dlg"].strip() and "点一下屏幕" not in st["dlg"]:
            if i >= 4:
                break

    final = page.evaluate("""() => ({
      dlg: (document.getElementById('dlgBox') || {}).textContent || '',
      log: window.__audioLog || [],
      fetchErrs: window.__errs || [],
    })""")
    ctx.close()
    browser.close()

print("\n=== 对话区 ===")
print(final["dlg"][:200].replace("\n", " | "))
print(f"\n=== 音频调用共 {len(final['log'])} 次 ===")
ok = sum(1 for r in final["log"] if r.get("playing"))
bad = [r for r in final["log"] if r.get("playRejected") or r.get("errorCode")]
print(f"成功播放 {ok} 次 | 异常 {len(bad)} 次")
for r in bad[:5]:
    print("   异常:", r)
