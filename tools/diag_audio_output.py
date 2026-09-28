"""定位"有文字没声音"：检查 TTS 接口与对话流返回的音频是否有效。"""
import base64
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8765"


def post(path, payload, timeout=90):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def head_info(b: bytes) -> str:
    if b[:4] == b"RIFF":
        import io
        import wave
        with wave.open(io.BytesIO(b), "rb") as w:
            return (f"WAV {w.getnchannels()}ch {w.getframerate()}Hz "
                    f"{w.getnframes() / w.getframerate():.2f}s")
    if b[:3] == b"ID3":
        return "MP3(ID3)"
    return f"未知头 {b[:8]!r}"


print("=== 1) /api/tts 直接合成 ===")
try:
    d = post("/api/tts", {"text": "红烧肉真是个美味的选择"})
    print("ok:", d.get("ok"), "format:", d.get("format"))
    if d.get("audio_b64"):
        raw = base64.b64decode(d["audio_b64"])
        print(f"字节数: {len(raw)} → {head_info(raw)}")
    else:
        print("没有音频数据:", d)
except Exception as exc:
    print("失败:", exc)

print("\n=== 2) /api/dialogue/stream 的 audio 事件 ===")
req = urllib.request.Request(BASE + "/api/dialogue/stream",
                             data=json.dumps({"message": "我吃完了"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
t0 = time.time()
buf, events = "", []
with urllib.request.urlopen(req, timeout=120) as resp:
    for line in resp:
        buf += line.decode("utf-8", "replace")
        if not line.strip() and buf.strip():
            ev, data = None, ""
            for l in buf.split("\n"):
                if l.startswith("event:"):
                    ev = l[6:].strip()
                elif l.startswith("data:"):
                    data += l[5:].strip()
            buf = ""
            if ev and data:
                try:
                    events.append((ev, json.loads(data), time.time() - t0))
                except Exception:
                    pass

for ev, d, dt in events:
    if ev == "audio":
        b64 = d.get("audio_b64") or ""
        raw = base64.b64decode(b64) if b64 else b""
        extra = head_info(raw) if raw else "❌ 无音频"
        print(f"  [{dt:5.1f}s] audio fmt={d.get('format')!r} cached={d.get('cached')} "
              f"ack={d.get('ack')} 文本={d.get('text')!r} → {extra}")
    elif ev == "reply":
        print(f"  [{dt:5.1f}s] reply {d.get('text')!r}")
    else:
        print(f"  [{dt:5.1f}s] {ev}")
