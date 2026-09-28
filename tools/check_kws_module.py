"""验证 sherpa KWS 检测器模块：唤醒词命中、无关语音不误触发。"""
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice.sherpa_kws import kws_detector  # noqa: E402

PS = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

print("模型可用:", kws_detector.status()["available"])
print("加载:", kws_detector.warmup(), "错误:", kws_detector.load_error())


def synth(text: str, tag: str) -> Path | None:
    raw = Path(rf"C:\Users\hp\AppData\Local\Temp\kwsmod_{tag}.wav")
    subprocess.run([str(PS), "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=0; "
                    f"$s.SetOutputToWaveFile('{raw}'); $s.Speak('{text}'); $s.Dispose()"],
                   capture_output=True)
    if not raw.exists():
        return None
    from app.voice.convert import to_16k_wav
    out = raw.with_name(f"kwsmod_{tag}_16k.wav")
    out.write_bytes(to_16k_wav(raw.read_bytes()))
    return out


def feed_case(path: Path, words, session, chunk_ms=200):
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert rate == 16000, rate
    step = int(16000 * chunk_ms / 1000)
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step].tobytes()
        r = kws_detector.feed(session, chunk, words=words, cooldown_sec=1.0)
        if r.get("matched"):
            return True, r
    return False, {}


words = ["你好饭崽", "你好正念", "饭崽"]
cases = [("你好饭崽", True), ("你好正念", True), ("饭崽", True),
         ("今天中午吃了红烧肉和米饭", False), ("我们几点出发去公园", False)]
passed = 0
for i, (text, expect) in enumerate(cases):
    path = synth(text, f"c{i}")
    if path is None:
        print("  无中文音色，跳过")
        break
    matched, info = feed_case(path, words, f"s{i}")
    ok = matched == expect
    passed += ok
    print(f"  [{'OK ' if ok else 'FAIL'}] 「{text}」→ 命中={matched} "
          f"词={info.get('word')!r}（期望 {expect}）")

st = kws_detector.status()
print(f"\n通过 {passed}/{len(cases)}；hits={st['hits']} last_word={st['last_word']!r} "
      f"最后电平 RMS={st['last_level']}")
