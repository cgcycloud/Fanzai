"""对话响应延迟实测：拆解 识别 → 首字 → 首句语音 → 结束 各阶段耗时。

对照 README 的宣称（首句文本 0.05s / 首句语音约 2s），定位真实瓶颈。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8765"
MESSAGE = sys.argv[1] if len(sys.argv) > 1 else "我今天中午吃了红烧肉，有点担心血糖"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def probe(message: str, tag: str):
    req = urllib.request.Request(
        f"{BASE}/api/dialogue/stream",
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    first_reply = first_audio = None
    replies, audios = [], []
    done = None
    with urllib.request.urlopen(req, timeout=120) as resp:
        buf = ""
        for raw in resp:
            line = raw.decode("utf-8", "replace")
            buf += line
            if not line.strip():
                # 事件结束（空行分隔）
                ev, data = None, ""
                for l in buf.split("\n"):
                    if l.startswith("event:"):
                        ev = l[6:].strip()
                    elif l.startswith("data:"):
                        data += l[5:].strip()
                buf = ""
                if not data:
                    continue
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                now = time.time() - t0
                if ev == "reply" and d.get("text"):
                    replies.append(d["text"])
                    if first_reply is None:
                        first_reply = now
                elif ev == "audio":
                    audios.append(bool(d.get("audio_b64")))
                    if first_audio is None and d.get("audio_b64"):
                        first_audio = now
                    # 生成等待期已不再有「即时回应」填充语（ACK_TEXTS 已删除），
                    # 所以这里没有 ack 分支可打点；等待只由前端动画表示。
                elif ev == "done":
                    done = now
    print(f"  [{tag}] 首句文本 {first_reply * 1000 if first_reply else -1:.0f}ms | "
          f"首句语音 {first_audio * 1000 if first_audio else -1:.0f}ms | "
          f"全部结束 {done * 1000 if done else -1:.0f}ms")
    print(f"         回复：{' / '.join(replies)}")
    return first_reply, first_audio, done


print(f"消息：{MESSAGE!r}  轮次：{ROUNDS}")
for i in range(ROUNDS):
    probe(MESSAGE, f"轮{i + 1}")
    time.sleep(1.0)
