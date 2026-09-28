"""验证 sherpa-onnx KWS：模型能唤醒 + 我能把中文唤醒词正确转成关键词行。"""
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODEL = (Path(r"C:\Users\hp\Desktop\mindful_meal\models")
         / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
ENC = MODEL / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
DEC = MODEL / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
JOI = MODEL / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"
TOK = MODEL / "tokens.txt"

tokens = set()
for line in TOK.read_text(encoding="utf-8").splitlines():
    parts = line.rsplit(" ", 1)
    if parts:
        tokens.add(parts[0])
print(f"tokens 表: {len(tokens)} 个")

INITIALS = ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
            "j", "q", "x", "r", "z", "c", "s", "y", "w"]


def split_syllable(syl: str) -> list[str]:
    """拼音音节 → 声母 + 韵母（模型 token 即这两部分）。无声母则只返回韵母。"""
    for ini in INITIALS:
        if syl.startswith(ini) and len(syl) > len(ini):
            return [ini, syl[len(ini):]]
    return [syl]


def keyword_line(text: str) -> tuple[str, list[str]]:
    """中文 → KWS 关键词行；返回 (关键词行, 缺失的 token 列表)。

    注意：带声调的拼音（ǐ ǎo àn）本身就是非 ASCII，不能用 isascii() 过滤，
    只能拿模型 tokens 表来校验。
    """
    from pypinyin import Style, lazy_pinyin
    syls = lazy_pinyin(text, style=Style.TONE, errors="ignore")
    toks: list[str] = []
    for s in syls:
        s = str(s).strip()
        if not s or not any(ch.isalpha() for ch in s):
            continue
        toks.extend(split_syllable(s))
    missing = [t for t in toks if t not in tokens]
    return " ".join(toks) + " @" + text, missing


print("\n关键词生成检查:")
for w in ("你好饭崽", "你好正念", "饭崽", "小爱同学"):
    line, missing = keyword_line(w)
    flag = "OK " if not missing else "缺token"
    print(f"  [{flag}] {w} → {line!r}" + (f" 缺: {missing}" if missing else ""))

kw_file = MODEL / "keywords.txt"
spotter = sherpa_onnx.KeywordSpotter(
    tokens=str(TOK), encoder=str(ENC), decoder=str(DEC), joiner=str(JOI),
    keywords_file=str(kw_file), num_threads=2, sample_rate=16000,
    keywords_score=1.0, keywords_threshold=0.25,
)
print("\n模型自带测试音频:")
for wav in sorted((MODEL / "test_wavs").glob("*.wav"))[:8]:
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    s = spotter.create_stream()
    s.accept_waveform(rate, samples)
    while spotter.is_ready(s):
        spotter.decode_stream(s)
    res = spotter.get_result(s)
    print(f"  {wav.name}: {res!r}")

# 关键验证：合成「你好饭崽」→ 用我生成的关键词行唤醒
print("\n用自生成关键词验证「你好饭崽」:")
line, missing = keyword_line("你好饭崽")
ps = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
wav = Path(r"C:\Users\hp\AppData\Local\Temp\kws_test.wav")
subprocess.run([str(ps), "-NoProfile", "-Command",
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=-1; "
                f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('你好饭崽'); $s.Dispose()"],
               capture_output=True)
from app.voice.convert import to_16k_wav  # noqa: E402
pcm16 = to_16k_wav(wav.read_bytes())
tmp = wav.with_name("kws_test_16k.wav")
tmp.write_bytes(pcm16)
with wave.open(str(tmp), "rb") as w:
    samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
s = spotter.create_stream(keywords=line)     # 按会话传自定义关键词
step = 1600                                   # 100ms 一片，模拟流式
hit = None
for i in range(0, len(samples), step):
    s.accept_waveform(16000, samples[i:i + step])
    while spotter.is_ready(s):
        spotter.decode_stream(s)
    r = spotter.get_result(s)
    if r:
        hit = r
        break
print(f"  关键词行: {line!r}")
print(f"  结果: {hit!r}")
print("  → ", "唤醒成功" if hit else "未唤醒")
