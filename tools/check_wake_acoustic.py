"""真实声学回环：扬声器播放「你好饭崽」→ 浏览器真麦克风收音 → 唤醒判定。

与 check_wake_browser.py 的区别：不使用假麦克风（不注入音频文件），
而是真的从扬声器放出来、由系统麦克风阵列拾取，验证真实使用场景。

⚠️ 注意：笔记本"内置扬声器 → 内置麦克风阵列"这条通路常常严重失真
（Windows 麦克风阵列的 AEC/降噪/AGC + 近距离大音量），实测同一段音频
Vosk 与 Paraformer 都只能听出乱码，此时唤醒不会命中 —— 这是采集硬件问题，
不是唤醒逻辑问题。回环验证建议用外接扬声器/麦克风，或直接用
check_wake_browser.py 注入干净音频验证软件链路。
参考判定方法：同时录下麦克风收到的声音，若两个 ASR 引擎都听不对，
即可确定是声学通路问题而非检测逻辑问题。
"""
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8765"
PHRASE = Path(r"C:\Users\hp\AppData\Local\Temp\wake_browser_16k.wav")
SPEAKER_HINT = "Realtek"


def pick_speaker():
    """挑一个真实扬声器（默认输出是蓝牙耳机，麦克风收不到）。"""
    for i, d in enumerate(sd.query_devices()):
        name = d["name"]
        if d["max_output_channels"] > 0 and SPEAKER_HINT in name and "Speaker" in name:
            return i, name
    return None, None


def load_phrase(rate: int) -> np.ndarray:
    with wave.open(str(PHRASE), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        src_rate = w.getframerate()
    audio = pcm / 32768.0
    if src_rate != rate:                      # 线性重采样到设备采样率
        ratio = src_rate / rate
        n = int(len(audio) / ratio)
        idx = np.arange(n) * ratio
        i0 = np.floor(idx).astype(int)
        i1 = np.minimum(i0 + 1, len(audio) - 1)
        audio = audio[i0] + (audio[i1] - audio[i0]) * (idx - i0)
    peak = float(np.max(np.abs(audio))) or 1.0
    return (audio / peak * 0.95).astype(np.float32)   # 拉满音量，确保麦克风收得到


def wake_status():
    import urllib.request
    with urllib.request.urlopen(f"{BASE}/api/agent/wake", timeout=5) as r:
        return json.loads(r.read())


dev, dev_name = pick_speaker()
if dev is None:
    print("找不到 Realtek 扬声器，无法做声学回环")
    sys.exit(1)
out_rate = int(sd.query_devices(dev)["default_samplerate"])
print(f"播放设备: [{dev}] {dev_name} @ {out_rate}Hz")
audio = load_phrase(out_rate)
print(f"音频长度: {len(audio) / out_rate:.2f}s")

before = wake_status()["detector"]["hits"]
print(f"唤醒前 hits = {before}")

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="msedge",
        headless=True,
        args=["--use-fake-ui-for-media-stream",           # 只自动授权，不替换麦克风！
              "--autoplay-policy=no-user-gesture-required"],
    )
    ctx = browser.new_context(permissions=["microphone"])
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    diag = page.evaluate("""() => ({
      wakeOn: typeof _wakeOn !== 'undefined' ? _wakeOn : 'n/a',
      wakeRate: typeof _wakeRate !== 'undefined' ? _wakeRate : 'n/a',
      ctxState: (typeof _wakeCtx !== 'undefined' && _wakeCtx) ? _wakeCtx.state : 'none',
      needsGesture: typeof _wakeNeedsGesture !== 'undefined' ? _wakeNeedsGesture : 'n/a',
      status: (document.getElementById('runStatus') || {}).textContent || '',
      micLabel: navigator.mediaDevices ? 'ok' : 'no-mediaDevices',
    })""")
    print("页面诊断:", diag)

    # 真实播放 3 次（间隔 2s），每次都看服务端"听到了什么"
    for round_no in range(1, 4):
        print(f"  第 {round_no} 次播放…")
        sd.play(audio, out_rate, device=dev)
        sd.wait()
        time.sleep(2.0)
        st = wake_status()["detector"]
        print(f"    服务端听到: {st.get('last_heard')!r}  (hits={st['hits']})")
        if st["hits"] > before:
            break

    time.sleep(1.0)
    after = wake_status()["detector"]["hits"]
    st = wake_status()["detector"]
    page.screenshot(path=str(Path(__file__).resolve().parents[1]
                             / "gui-test-screenshots" / "verify_wake_acoustic.png"))
    print(f"唤醒后 hits = {after}  (last_text={st.get('last_text')!r}, last_word={st.get('last_word')!r})")
    ctx.close()
    browser.close()

print("控制台错误:", errors if errors else "无")
print("=== 真实声学唤醒:", "通过" if after > before else "未触发", "===")
sys.exit(0 if after > before else 1)
