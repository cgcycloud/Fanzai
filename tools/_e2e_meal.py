"""End-to-end verification of the new meal lifecycle + busy-guard, against the
running server (real HTTP + real DB)."""
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# 允许指向另一个端口（例：用 8766 跑一份"改了代码但没重启正式服务"的实例）
B = os.environ.get("MINDFUL_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
UID = "e2e-meal"
fails = []


def check(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def meal():
    return requests.get(f"{B}/api/dialogue/meal", params={"user_id": UID}, timeout=20).json()


def say(text):
    """走流式对话接口（前端用的就是它），把回复收集起来。"""
    with requests.post(f"{B}/api/dialogue/stream",
                       json={"message": text, "user_id": UID},
                       stream=True, timeout=120) as r:
        ev, replies, done = None, [], None
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                if ev == "reply":
                    replies.append(d.get("text", ""))
                elif ev == "done":
                    done = d
        return "".join(replies), (done or {})


# ---- 收尾任何遗留的一餐，确保从干净状态开始 ----
requests.post(f"{B}/api/dialogue/meal/finish", json={"user_id": UID, "reason": "cleanup"},
              timeout=20)
time.sleep(0.5)

print("=== 1) 起始状态：没有进行中的一餐 ===")
st = meal()
check("phase=idle", st.get("phase") == "idle", json.dumps(st, ensure_ascii=False))

print("\n=== 2) 用户说「我要吃饭了」→ 自动进入餐前 ===")
reply, done = say("我要吃饭了")
st = meal()
check("meal active", st.get("active") is True, json.dumps(st, ensure_ascii=False))
check("phase=pre_meal（餐前）", st.get("phase") == "pre_meal", st.get("phase"))
print(f"     AI: {reply[:80]}")

print("\n=== 3) 再聊几轮，阶段**不应**自动跑到餐中（旧版按轮数推进）===")
for t in ("今天做了个番茄炒蛋", "还有一个青菜"):
    reply, _ = say(t)
print(f"     最后一轮 AI: {reply[:80]}")
st = meal()
check("仍然是餐前", st.get("phase") == "pre_meal", st.get("phase"))
check("轮数已累计", (st.get("turns") or 0) >= 3, f"turns={st.get('turns')}")

print("\n=== 4) 用户说「开始吃了」→ 进入餐中 ===")
reply, _ = say("我开吃了")
st = meal()
check("phase=mid_meal（餐中）", st.get("phase") == "mid_meal", st.get("phase"))
print(f"     AI: {reply[:80]}")

print("\n=== 5) 餐中提醒要围绕慢下来/饱足，且不谈餐前那套 ===")
reply, _ = say("这个肉有点咸")
check("回复非空", bool(reply.strip()))
print(f"     AI: {reply[:100]}")

print("\n=== 6) 用户说「我吃完了」→ 进入餐后并**统计一次** ===")
# 直接数数据库里的记录（/api/report 默认只看 user_id=default，而本用例用的是 e2e-meal）
from app.core.db import HealthStore                                   # noqa: E402

_store = HealthStore()
from datetime import datetime, timedelta                              # noqa: E402


def count_events(etype):
    rows = _store.query_event_range(etype, datetime.now() - timedelta(hours=1),
                                    datetime.now() + timedelta(hours=1))
    return len([r for r in rows if (r.get("user_id") or "default") == UID])


before_food, before_sum = count_events("food_residual"), count_events("meal_summary")
reply, done = say("我吃完了")
check("done.finished=True", (done or {}).get("finished") is True, str(done)[:120])
st = meal()
check("会话已关闭（统计完成）", st.get("active") is False, json.dumps(st, ensure_ascii=False))
print(f"     AI: {reply[:80]}")

time.sleep(1.0)
after_food, after_sum = count_events("food_residual"), count_events("meal_summary")
check("food_residual +1（算用餐次数用）", after_food == before_food + 1,
      f"{before_food} -> {after_food}")
check("meal_summary +1（一餐总结）", after_sum == before_sum + 1,
      f"{before_sum} -> {after_sum}")

print("\n=== 7) 幂等：再调一次收尾不应重复统计 ===")
r = requests.post(f"{B}/api/dialogue/meal/finish",
                  json={"user_id": UID, "reason": "again"}, timeout=20).json()
check("第二次收尾 recorded=False", r.get("recorded") is False, json.dumps(r, ensure_ascii=False))
check("food_residual 没有再次 +1", count_events("food_residual") == after_food,
      f"{after_food} -> {count_events('food_residual')}")
check("meal_summary 没有再次 +1", count_events("meal_summary") == after_sum,
      f"{after_sum} -> {count_events('meal_summary')}")

print("\n=== 8) 忙碌守卫：用户说话时 AI 不得主动开口 ===")
# 注意：这个脚本是**独立进程**，本地 import 的 display_state 与服务端不是同一个实例，
# 所以必须走 HTTP 读服务端的状态（/api/device/listen 会回 busy）。
r1 = requests.post(f"{B}/api/device/listen",
                   json={"active": True, "reason": "listening"}, timeout=20).json()
check("服务端置忙（用户在说话）", r1.get("busy") is True, json.dumps(r1, ensure_ascii=False))
check("设备屏表情切到 listening", r1.get("face") == "listening", str(r1.get("face")))

r2 = requests.post(f"{B}/api/device/listen",
                   json={"active": True, "reason": "speaking"}, timeout=20).json()
check("播报中也算忙", r2.get("busy") is True, json.dumps(r2, ensure_ascii=False))

r3 = requests.post(f"{B}/api/device/listen", json={"active": False}, timeout=20).json()
check("解除后可开口", r3.get("busy") is False, json.dumps(r3, ensure_ascii=False))

print("\n=== 9) 唤醒应答：拿到的是「收干净了」的那条（不是掐断的残句）===")
ack = requests.get(f"{B}/api/agent/wake/ack", timeout=60).json()
check("接口有音频", ack.get("ok") is True and bool(ack.get("audio_b64")), str(ack)[:120])
if ack.get("audio_b64"):
    import base64

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.voice.tts_cache import _ack_ok, wav_stats

    stats = wav_stats(base64.b64decode(ack["audio_b64"]))
    check("时长/尾音达标（没被掐断）", _ack_ok(stats),
          ("stats=" + json.dumps(stats)) if stats else "无法解析")

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("MEAL E2E PASSED")
