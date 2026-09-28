"""设备屏幕的共享状态 —— 表情状态、页面、最近对话、实时指标。

线程安全：屏幕渲染线程读取，API/对话线程写入。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

# 表情状态与语音同步：待机 → 倾听 → 思考 → 说话 → 待机
FACE_IDLE = "idle"
FACE_LISTENING = "listening"
FACE_THINKING = "thinking"
FACE_SPEAKING = "speaking"

# 新头像组件使用的情绪映射；中性不覆盖待机状态。
EMOTION_TO_FACE = {
    "happy": "relaxed", "sad": "aggrieved", "angry": "irritated",
    "surprise": "surprise", "love": "anticipation", "sleepy": "drowsy",
    "cry": "aggrieved", "wink": "wink", "cool": "relaxed", "shy": "reassured",
    "anxiety": "anxiety", "irritated": "irritated", "hatred": "hatred",
    "aggrieved": "aggrieved", "anticipation": "anticipation", "blessing": "blessing",
    "alert": "alert", "reassured": "reassured", "relaxed": "relaxed", "drowsy": "drowsy",
}

PAGES = ["face", "camera", "stats"]
PAGE_TITLES = {"face": "表情", "camera": "摄像头", "stats": "健康报告"}


@dataclass
class Turn:
    user: str
    assistant: str
    ts: float = field(default_factory=time.time)


class DisplayState:
    """设备屏幕状态（单例，供渲染线程与 API 共享）。"""

    def __init__(self, page: str = "face"):
        self._lock = threading.RLock()
        self.page = page if page in PAGES else "face"

        self._face = FACE_IDLE
        self._face_override: Optional[str] = None      # 情绪表情（由 AI 回复情感驱动）
        self._override_until = 0.0
        self._speaking_until = 0.0                     # 说话状态预期结束时间
        self._turns: Deque[Turn] = deque(maxlen=2)     # 只保留最近两轮
        self._metrics: Dict[str, object] = {
            "chews_per_min": None, "chew_level": None,
            "chew_count": None,                 # 累计咀嚼次数（肌肉法检测器）
            "bites_per_min": None, "eat_level": None,
            "emotion": None, "emotion_key": None, "face_count": None,
            "motion": None, "vision": False, "metrics_ts": None,
        }
        self._status_line = ""
        self._ai_configured = False
        # 「正在忙」标志（见 set_listening / set_busy / is_busy）
        self._listening = False
        self._busy_until: Dict[str, float] = {}
        from ..config import SCREEN_H
        self._scroll: Dict[str, float] = {p: 0.0 for p in PAGES}
        self._content_h: Dict[str, float] = {p: float(SCREEN_H) for p in PAGES}
        self._scroll_dirty = False

    # ---------- 表情状态 ----------
    def face_state(self) -> str:
        with self._lock:
            if self._face == FACE_SPEAKING and time.time() > self._speaking_until:
                self._face = FACE_IDLE
            if self._face == FACE_IDLE and self._face_override and time.time() < self._override_until:
                return self._face_override
            return self._face

    def set_face(self, state: str) -> None:
        with self._lock:
            self._face = state

    def set_emotion(self, emotion: str, hold_sec: float = 6.0) -> None:
        """AI 回复的情感 → 短暂覆盖待机表情（说话结束后仍保留一会儿）。

        中性/未知情感不覆盖：否则会出现一张"面瘫脸"，把待机轮换表情顶掉。
        """
        with self._lock:
            self._face_override = EMOTION_TO_FACE.get(emotion)  # None = 不覆盖
            self._override_until = time.time() + hold_sec

    def mark_speaking(self, est_seconds: float = 4.0) -> None:
        with self._lock:
            self._face = FACE_SPEAKING
            self._speaking_until = time.time() + max(1.0, est_seconds)

    def end_speaking(self) -> None:
        with self._lock:
            self._face = FACE_IDLE

    # ---------- 页面 ----------
    def set_page(self, page: str) -> str:
        with self._lock:
            if page in PAGES:
                self.page = page
            return self.page

    def next_page(self) -> str:
        with self._lock:
            i = PAGES.index(self.page)
            self.page = PAGES[(i + 1) % len(PAGES)]
            return self.page

    def get_page(self) -> str:
        with self._lock:
            return self.page

    # ---------- 「正在忙」：阻止 AI 主动插话 ----------
    # 为什么需要一个独立于"表情"的忙标志：
    #   * 表情会被"情绪覆盖"顶掉（set_emotion 后 face_state() 返回 happy/焦虑 等），
    #     所以 `face_state() == "idle"` 这种判断既会误判也会漏判；
    #   * 用户**正在说话**这件事只有浏览器知道（录音还没上传），必须由前端上报，
    #     否则服务端一直以为设备空闲 —— 结果就是用户话说到一半被 AI 主动开口打断。
    def set_listening(self, active: bool) -> None:
        with self._lock:
            self._listening = bool(active)
            if active:
                self._busy_until["listening"] = 0.0     # 0 = 由前端显式结束
            else:
                self._busy_until.pop("listening", None)

    def set_busy(self, reason: str, busy: bool, ttl: float = 0.0) -> None:
        """外部声明的忙碌（前端播放语音、对话进行中等）。

        ttl > 0 时到点自动失效，避免前端异常退出后永久卡住"忙"。
        """
        with self._lock:
            if busy:
                self._busy_until[reason] = (time.time() + ttl) if ttl > 0 else 0.0
            else:
                self._busy_until.pop(reason, None)

    def is_busy(self) -> bool:
        """是否应当阻止 AI 主动开口（用户在说话 / 正在思考 / 正在播报）。"""
        with self._lock:
            if self._listening:
                return True
            if self._face in (FACE_LISTENING, FACE_THINKING):
                return True
            if self._face == FACE_SPEAKING and time.time() < self._speaking_until:
                return True
            now = time.time()
            for reason, until in self._busy_until.items():
                if until == 0.0 or now < until:
                    return True
            return False

    def busy_reason(self) -> str:
        """当前忙碌原因（诊断用）。"""
        with self._lock:
            if self._listening:
                return "listening"
            if self._face in (FACE_LISTENING, FACE_THINKING, FACE_SPEAKING):
                return self._face
            now = time.time()
            for reason, until in self._busy_until.items():
                if until == 0.0 or now < until:
                    return reason
            return ""

    # ---------- 对话与指标 ----------
    def add_turn(self, user: str, assistant: str) -> None:
        with self._lock:
            self._turns.append(Turn(user=user, assistant=assistant))

    def turns(self) -> List[Turn]:
        with self._lock:
            return list(self._turns)

    def set_metrics(self, **kw: object) -> None:
        with self._lock:
            for k, v in kw.items():
                if k in self._metrics:
                    self._metrics[k] = v

    def metrics(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._metrics)

    def set_status_line(self, text: str) -> None:
        with self._lock:
            self._status_line = text or ""

    def status_line(self) -> str:
        with self._lock:
            return self._status_line

    # ---------- 纵向滚动（详细页内容超出屏幕时上下滑动查看）----------
    def add_scroll(self, page: str, delta: float) -> float:
        from ..config import SCREEN_H

        with self._lock:
            if page not in self._scroll:
                return 0.0
            max_scroll = max(0.0, self._content_h.get(page, float(SCREEN_H)) - SCREEN_H)
            self._scroll[page] = max(0.0, min(max_scroll, self._scroll[page] + delta))
            self._scroll_dirty = True
            return self._scroll[page]

    def get_scroll(self, page: str) -> float:
        with self._lock:
            return self._scroll.get(page, 0.0)

    def set_content_height(self, page: str, height: float) -> None:
        from ..config import SCREEN_H

        with self._lock:
            self._content_h[page] = max(float(SCREEN_H), height)
            # 内容变短时收紧滚动位置
            max_scroll = max(0.0, self._content_h[page] - SCREEN_H)
            if self._scroll.get(page, 0.0) > max_scroll:
                self._scroll[page] = max_scroll

    @property
    def scroll_dirty(self) -> bool:
        with self._lock:
            v = self._scroll_dirty
            self._scroll_dirty = False
            return v

    def set_ai_configured(self, ok: bool) -> None:
        with self._lock:
            self._ai_configured = bool(ok)

    def ai_configured(self) -> bool:
        with self._lock:
            return self._ai_configured

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "page": self.page,
                "face": self.face_state(),
                "turns": [{"user": t.user, "assistant": t.assistant} for t in self._turns],
                "metrics": dict(self._metrics),
                "status": self._status_line,
                "ai_configured": self._ai_configured,
                "scroll": dict(self._scroll),
                "content_h": dict(self._content_h),
            }


# 全局单例
display_state = DisplayState()
