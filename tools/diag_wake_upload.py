"""插桩诊断：浏览器到底上传了多大的音频、页面状态如何、服务端识别到什么。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"
MIC = r"C:\Users\hp\AppData\Local\Temp\wake_browser_mic.wav"


def node_wake():
    with urllib.request.urlopen(f"{BASE}/api/agent/wake", timeout=5) as r:
        return json.loads(r.read())


feeds = []
with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="msedge", headless=True,
        args=["--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-audio-capture={MIC}",
              "--autoplay-policy=no-user-gesture-required"],
    )
    ctx = browser.new_context(permissions=["microphone"])
    page = ctx.new_page()

    def on_req(r):
        if "/api/agent/wake/feed" in r.url:
            try:
                n = len(r.post_data_buffer or b"")
            except Exception:
                n = -1
            feeds.append(n)
    page.on("request", on_req)
    page.goto(BASE, wait_until="domcontentloaded")

    for i in range(8):
        page.wait_for_timeout(3000)
        st = page.evaluate("""() => ({
          status: (document.getElementById('runStatus') || {}).textContent || '',
          wakeOn: typeof _wakeOn !== 'undefined' ? _wakeOn : null,
          rate: typeof _wakeRate !== 'undefined' ? _wakeRate : null,
          floor: typeof _wakeFloor !== 'undefined' ? _wakeFloor : null,
          uploading: typeof _wakeUploading !== 'undefined' ? _wakeUploading : null,
          pcm: typeof _wakePcm !== 'undefined' ? _wakePcm.length : null,
          ackPlaying: typeof _wakeAckPlaying !== 'undefined' ? _wakeAckPlaying : null,
          recording: typeof _recording !== 'undefined' ? _recording : null,
        })""")
        sizes = [n for n in feeds if n >= 0]
        print(f"t={3 * (i + 1):2d}s feeds={len(feeds)} 分片字节(近5)={sizes[-5:]} "
              f"状态={st['status'][:30]!r}")
        print(f"        wakeOn={st['wakeOn']} rate={st['rate']} floor={st['floor']} "
              f"uploading={st['uploading']} 待发={st['pcm']} 应答中={st['ackPlaying']} "
              f"录音中={st['recording']}")
    ctx.close()
    browser.close()

st = node_wake()["detector"]
print(f"\n服务端: hits={st['hits']} last_heard={st.get('last_heard')!r} "
      f"hits_last={st.get('last_word')!r}")
print(f"上传分片总数={len(feeds)} 总字节={sum(n for n in feeds if n > 0)}")
