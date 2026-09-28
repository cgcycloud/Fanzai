"""反面验证：无关语音 / 环境噪声不能误唤醒。

用户实测问题："环境不安静时会识别其他声音"（误唤醒）。这个脚本把
  1) 与唤醒词无关的普通话语音（TTS 合成）
  2) 宽带噪声（模拟嘈杂环境）
当作浏览器麦克风输入，断言唤醒次数保持为 0，并打印服务端"听到了什么"，
用来区分"被噪声刷醒"和"根本没收到声音"。
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

PS = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
BASE = "http://127.0.0.1:8765"
RATE = 16000


def node_wake():
    with urllib.request.urlopen(f"{BASE}/api/agent/wake", timeout=5) as r:
        return json.loads(r.read())


def wav_write(path: Path, pcm: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def make_unrelated_speech() -> Path | None:
    """合成与唤醒词无关的普通话语音（最容易造成"同音误唤醒"的素材）。"""
    raw = Path(tempfile.gettempdir()) / "neg_raw.wav"
    text = "今天天气不错我们出去走走吧晚饭想吃点清淡的"
    subprocess.run([str(PS), "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=0; "
                    f"$s.SetOutputToWaveFile('{raw}'); $s.Speak('{text}'); $s.Dispose()"],
                   capture_output=True, check=False)
    if not raw.exists():
        return None
    from app.voice.convert import to_16k_wav
    pcm = to_16k_wav(raw.read_bytes())
    tmp = Path(tempfile.gettempdir()) / "neg_16k.wav"
    tmp.write_bytes(pcm)
    with wave.open(str(tmp), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    # 前后加静音，并重复两遍（拉长暴露时间）
    silence = np.zeros(int(RATE * 1.5), dtype=np.float32)
    looped = np.concatenate([silence, a, silence, a, silence])
    return wav_write(Path(tempfile.gettempdir()) / "neg_speech.wav", looped)


def make_noise() -> Path:
    """宽带噪声 + 缓慢起伏（模拟嘈杂环境/多人说话的能量包络）。"""
    rng = np.random.default_rng(7)
    n = int(RATE * 12)
    noise = rng.normal(0, 0.08, n).astype(np.float32)
    # 让噪声有"忽大忽小"的包络，更像真实环境而不是恒定电流声
    env = (0.5 + 0.5 * np.sin(np.linspace(0, 12 * np.pi, n))).astype(np.float32)
    burst = (rng.random(n) < 0.002).astype(np.float32)      # 偶发响声（关门/碗筷）
    sig = noise * (0.6 + env) + burst * rng.normal(0, 0.25, n).astype(np.float32)
    return wav_write(Path(tempfile.gettempdir()) / "neg_noise.wav", sig)


def run_case(label: str, wav: Path, seconds: int = 14) -> bool:
    before = node_wake()["detector"]["hits"]
    feeds: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--use-fake-ui-for-media-stream",           # 自动授权麦克风
                  "--use-fake-device-for-media-stream",       # 必须有：否则用的是真实麦克风，
                                                              # 注入的噪声/语音素材根本不会被采集
                  f"--use-file-for-fake-audio-capture={wav}",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        ctx = browser.new_context(permissions=["microphone"])
        page = ctx.new_page()
        page.on("request", lambda r: feeds.append(r.url)
                if "/api/agent/wake/feed" in r.url else None)
        page.goto(BASE, wait_until="domcontentloaded")
        for _ in range(seconds // 2):
            page.wait_for_timeout(2000)
        ctx.close()
        browser.close()

    st = node_wake()["detector"]
    after = st["hits"]
    heard = st.get("last_heard") or ""
    no_false_wake = after == before
    # 测试有效性：必须确认音频真的被上传过 —— 否则"没误唤醒"只是因为根本没送音频，
    # 那样的"通过"毫无意义（曾经被 6 秒启动标定期掩盖过一次）。
    meaningful = len(feeds) > 0
    ok = no_false_wake and meaningful
    print(f"[{'OK ' if ok else 'FAIL'}] {label}: 误唤醒 {after - before} 次 | "
          f"上传分片 {len(feeds)} 次 | 服务端最近听到 {heard!r}")
    if not meaningful:
        print("      ↑ 音频根本没上传，本次验证无效（噪声门可能过严）")
    return ok


if not PS.exists():
    print("非 Windows，跳过（需要系统 TTS 生成无关语音素材）")
    sys.exit(0)

speech = make_unrelated_speech()
results = []
if speech:
    results.append(run_case("无关普通话语音", speech))
results.append(run_case("宽带环境噪声", make_noise()))

print("\n=== 抗误唤醒:", "通过" if all(results) else "失败", "===")
sys.exit(0 if all(results) else 1)
