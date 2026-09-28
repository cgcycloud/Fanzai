"""对话长期记忆 —— 近期对话轮次 + AI 压缩摘要（超限时自动合并压缩）。"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from ..config import (PATHS, MEMORY_KEEP_RECENT_TURNS, MEMORY_RESERVE_TOKENS,
                      MEMORY_SEND_TOKENS, MEMORY_TOKEN_LIMIT)

# 压缩是"读-改-写"，同一份记忆同一时刻只允许一个线程做（见 compress_if_needed）
_COMPRESS_LOCK = threading.Lock()


class TextCompressor(Protocol):
    def chat(self, text: str, *, temperature: float = 0.7, timeout: float = 30) -> str: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2)


@dataclass
class ConversationMemory:
    path: Path
    token_limit: int = MEMORY_TOKEN_LIMIT
    reserve_tokens: int = MEMORY_RESERVE_TOKENS
    keep_recent_turns: int = MEMORY_KEEP_RECENT_TURNS
    user_id: str = "default"
    summary: str = ""
    turns: List[Dict[str, str]] = field(default_factory=list)
    # turn 结构: {user_text, assistant_reply, image_path, phase, timestamp}

    @classmethod
    def load(cls, user_id: str = "default", path: Optional[Path] = None) -> "ConversationMemory":
        """读这个用户的对话记忆。

        **token_limit / reserve_tokens / keep_recent_turns 只认 `app/config.py` 里的值**，
        不从文件里读：这几个是应用级设置，一旦跟着旧文件走，"把上下文调大到 128k"
        就会被几份老记忆文件悄悄按回旧上限（改了不生效的那种坑）。
        文件里残留的这几个字段会被忽略，下次 save() 自然消失。
        """
        if path is None:
            path = PATHS.data_dir / f"conversation_memory_{user_id}.json"
        if not path.exists():
            return cls(path=path, user_id=user_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls(path=path, user_id=user_id)
        return cls(
            path=path,
            user_id=str(data.get("user_id") or user_id),
            summary=str(data.get("summary") or ""),
            turns=list(data.get("turns") or []),
        )

    @property
    def usable_tokens(self) -> int:
        """超过这个规模就把旧对话压成摘要。

        取 **发送预算的 4 倍**（而不是顶到 `token_limit`=128k 才压）：
        每次真正发出去的只有"摘要 + 最近 `MEMORY_SEND_TOKENS`"，
        如果压缩线放在 128k，中间那几万 token 就既发不出去、也没进摘要 ——
        AI 会莫名其妙"忘了前面说过的话"。取 4 倍能保证发送窗口永远落在
        还留着原文的范围内：更早的都进摘要了，而且每轮 prefill 恒定在 2k 上下。
        `token_limit`（128k）仍作为硬上限，永远不会越过。
        """
        ceiling = max(4_000, self.token_limit - self.reserve_tokens)
        return max(4_000, min(ceiling, MEMORY_SEND_TOKENS * 4))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "user_id": self.user_id,
                    "summary": self.summary,
                    "turns": self.turns,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def context_text(self, max_tokens: int | None = MEMORY_SEND_TOKENS) -> str:
        """拼给模型看的上下文：长期摘要 + 最近对话。

        `max_tokens` 是**这一轮实际发出去**的预算（默认 `MEMORY_SEND_TOKENS`）：
        太长会把每次请求的 prefill 拖慢（攒到 10 万 token 时首字要几十秒），
        所以从最近一轮往前累加、塞不下就不发了 —— 更早的内容已经进摘要，
        记忆本身仍然保留到 `MEMORY_TOKEN_LIMIT`（128k）才压缩。
        传 None 表示不设限（诊断/测试用）。
        """
        parts: list[str] = []
        if self.summary:
            parts.append("长期摘要：\n" + self.summary.strip())
        budget = None if max_tokens is None else max(200, int(max_tokens))
        if budget is not None and self.summary:
            budget -= estimate_tokens(self.summary)
        recent: list[str] = []
        if self.turns and (budget is None or budget > 0):
            # 从最后一轮往前收，收满预算为止（保证"最近说过的话"一定在里面）
            for idx, turn in enumerate(reversed(self.turns), 1):
                user_text = turn.get("user_text", "").strip()
                assistant_reply = turn.get("assistant_reply", "").strip()
                image_note = "；本轮包含摄像头图片" if turn.get("image_path") else ""
                phase_note = f" [阶段:{turn.get('phase', '')}]" if turn.get("phase") else ""
                block = f"{idx}. 用户：{user_text}{image_note}{phase_note}\n助手：{assistant_reply}"
                if budget is not None and estimate_tokens(block) > budget:
                    break
                if budget is not None:
                    budget -= estimate_tokens(block)
                recent.append(block)
        if recent:
            parts.append("最近对话：")
            parts.extend(reversed(recent))          # 放回时间顺序（旧 → 新）
        elif self.turns and budget is not None and budget <= 0:
            # 连一轮都塞不下（摘要太长）：至少带上最后一句用户原话
            last = self.turns[-1].get("user_text", "").strip()
            if last:
                parts.append(f"最近一轮用户说：{last}")
        return "\n\n".join(parts).strip()

    def token_count(self) -> int:
        """当前**全部**记忆的估算规模（含所有轮次，供压缩判断用）。"""
        return estimate_tokens(self.context_text(max_tokens=None))

    def add_turn(self, *, user_text: str, assistant_reply: str,
                 image_path: str | None = None, phase: str = "") -> None:
        self.turns.append({
            "user_text": user_text,
            "assistant_reply": assistant_reply,
            "image_path": image_path or "",
            "phase": phase,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.save()

    def compress_if_needed(self, client: TextCompressor) -> bool:
        """把超出窗口的旧对话压成摘要（返回是否真的压缩了）。

        并发安全：调用方是在后台线程里跑的，而对话线程可能正好在追加新一轮，
        所以**以磁盘上的最新内容为准**，并且写回前再读一次、把压缩期间新增的
        轮次接上 —— 否则会把新写入的那一轮覆盖掉（丢一轮对话）。

        摘要失败时**不压缩**（下次再试）：宁可暂时多留点原文，
        也不能把旧对话丢了、只写一句"已截断"。
        """
        with _COMPRESS_LOCK:
            fresh = self.load(self.user_id, path=self.path)
            if fresh.token_count() <= fresh.usable_tokens:
                return False
            if len(fresh.turns) <= fresh.keep_recent_turns:
                return False

            old_turns = fresh.turns[: -fresh.keep_recent_turns]
            recent_turns = fresh.turns[-fresh.keep_recent_turns:]
            old_text = "\n".join(
                f"用户：{t.get('user_text', '')}\n助手：{t.get('assistant_reply', '')}"
                for t in old_turns
            )
            prompt = (
                "请把以下设备长期对话上下文压缩成可继续用于陪伴对话的记忆摘要。"
                "必须保留：用户偏好、饮食习惯、慢性病信息、用药记录、情绪线索、重要健康事件。"
                "删除寒暄和重复内容。输出中文，结构清晰，尽量控制在 2000 字以内。\n\n"
                f"已有摘要：\n{fresh.summary or '无'}\n\n需要压缩的旧对话：\n{old_text}"
            )
            try:
                summary = client.chat(prompt, temperature=0.2, timeout=45).strip()
            except Exception as exc:
                print(f"[memory] 摘要压缩失败，保留原文下次再试: {exc}", flush=True)
                return False
            if not summary:
                return False

            # 写回前再读一次：把"压缩期间新追加的轮次"接在最近轮次后面
            latest = self.load(self.user_id, path=self.path)
            appended = latest.turns[len(fresh.turns):] if len(latest.turns) >= len(fresh.turns) else []
            fresh.summary = summary
            fresh.turns = recent_turns + appended
            fresh.save()

            # 让调用方手里的这份内存对象也跟上（它可能还会被继续用）
            self.summary, self.turns = fresh.summary, fresh.turns
            print(f"[memory] 已压缩：{len(old_turns)} 轮 → 摘要 "
                  f"（保留最近 {len(recent_turns)} 轮 + 新增 {len(appended)} 轮）", flush=True)
            return True

    # ---------- 面向引导对话的检索接口 ----------
    def get_recent_diet_summary(self, store, days: int = 7) -> str:
        """最近 N 天饮食相关对话/事件摘要（判断“上一餐是否短于 2 小时”等）。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        diet_turns = [
            t for t in self.turns
            if t.get("phase") in ("pre_meal", "mid_meal") and t.get("timestamp", "") >= cutoff
        ]
        if diet_turns:
            return "\n".join(
                f"{t.get('timestamp', '')}: {t.get('user_text', '')}" for t in diet_turns[-5:]
            )
        try:
            events = store.query_events_json(
                "select data_json from events where type='dialogue' and timestamp >= ? "
                "order by timestamp desc limit 10",
                (cutoff,),
            )
            texts = [e.get("user_text", "") for e in events if e.get("user_text")]
            return " ".join(texts) if texts else "近7天暂无饮食记录。"
        except Exception:
            return "饮食记录查询失败。"

    def get_emotion_trend(self, store, days: int = 30) -> Dict[str, Any]:
        """最近 N 天情绪标签分布。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            events = store.query_events_json(
                "select data_json from events where type='emotion' and timestamp >= ?",
                (cutoff,),
            )
        except Exception:
            return {"error": "无法查询情绪数据", "days": days}
        counts: Dict[str, int] = {}
        for e in events:
            emo = e.get("emotion", "unknown")
            counts[emo] = counts.get(emo, 0) + 1
        total = sum(counts.values())
        trend = {k: {"count": v, "ratio": round(v / total, 2)} for k, v in counts.items()} if total else {}
        return {"days": days, "total_entries": total, "trend": trend}
