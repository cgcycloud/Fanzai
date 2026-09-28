# 生成浏览器假麦克风音频：静音 + 「你好饭崽」 + 静音（供 Chromium --use-file-for-fake-audio-capture）
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice.convert import to_16k_wav  # noqa: E402

PS = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
RAW = r"C:\Users\hp\AppData\Local\Temp\wake_browser_raw.wav"
OUT = r"C:\Users\hp\AppData\Local\Temp\wake_browser_mic.wav"

subprocess.run([str(PS), "-NoProfile", "-Command",
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=-1; "
                f"$s.SetOutputToWaveFile('{RAW}'); $s.Speak('你好饭崽'); $s.Dispose()"],
               capture_output=True, check=False)

raw16k = to_16k_wav(Path(RAW).read_bytes())
tmp = Path(tempfile.gettempdir()) / "wake_browser_16k.wav"
tmp.write_bytes(raw16k)

with wave.open(str(tmp), "rb") as w:
    pcm = w.readframes(w.getnframes())
    rate = w.getframerate()

# 1.2s 静音 + 唤醒词 + 2.5s 静音（Chromium 会循环播放整个文件）
silence_a = b"\x00\x00" * int(rate * 1.2)
silence_b = b"\x00\x00" * int(rate * 2.5)
with wave.open(OUT, "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(rate)
    out.writeframes(silence_a + pcm + silence_b)

total = (len(silence_a) + len(pcm) + len(silence_b)) / 2 / rate
print(f"已生成 {OUT}\n  采样率 {rate}Hz 单声道 总长 {total:.1f}s（唤醒词在 1.2s 处）")
