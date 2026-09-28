"""一餐的生命周期（餐前 → 餐中 → 餐后 → 收尾统计）—— 取代"按对话轮数推进"的旧做法。

## 一餐是怎么走的（唯一的真相来源）

    空闲(IDLE) ──用户说"我要吃饭/开饭" **或** 摄像头连续看到在吃 ≥25s──▶ 餐中(MID)
    餐前(PRE)  ──用户说"开吃/开始吃了" **或** 摄像头连续看到在吃 ≥25s──▶ 餐中(MID)
    餐中(MID)  ──用户说"吃完了/不吃了" / 回答"对·嗯" / 静默 2 分钟后问一句──▶ 餐后(POST)
    餐后(POST) ──把"收尾这一句"说完──────────────────────────────▶ 统计一次，回 IDLE

* 说"我要吃饭"才进餐前（餐前是**准备吃**的那几步引导）；摄像头直接看到已经在吃时
  不再假装餐前，直接进餐中 —— 用户都已经在嚼了，再让他分辨"是胃饿还是心饿"很荒诞。
* **统计时机只有一个**：餐后收尾那一次（`finish()`）。带幂等保护，无论被用户话术、
  超时、还是接口重复调用触发，都只会落一条 `food_residual` + 一条 `meal_summary`。
* **没人吃/没回应也要落地**：连续 **10 分钟**既没在吃、也没说话（用户直接走了，
  或者问过他"吃完了吗"也不回应）→ 强制收尾并统计，否则"用餐次数"会永远停在 0；
  另有 45 分钟整餐上限兜底（一直在吃、一直在聊也不会挂过夜）。
* 阶段提示按阶段给**具体**的行为指令，而不是一句泛泛的"当前处于餐前"。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from . import interaction_config as ic


class MealPhase(str, Enum):
    IDLE = "idle"
    PRE = "pre_meal"
    MID = "mid_meal"
    POST = "post_meal"


PHASE_ZH = {
    MealPhase.IDLE: "空闲",
    MealPhase.PRE: "餐前",
    MealPhase.MID: "餐中",
    MealPhase.POST: "餐后",
}

# 各阶段给模型的**行为要求**（不是话术模板 —— 具体怎么说交给模型）
PHASE_GUIDE: Dict[MealPhase, str] = {
    MealPhase.PRE: (
        "当前是【餐前】：用户还没开始吃。"
        "这一阶段只做三件事，且一次只做一件：①先安抚/觉察（三次深呼吸或一句身体扫描）；"
        "②帮他分辨「是胃在饿，还是心在饿」；③问一句今天要吃什么，让注意力落到食物上。"
        "不要催他开始吃，也不要在这阶段谈咀嚼速度、几分饱；"
        "这三件事不必按顺序一次问完，夹在日常聊天里做也可以。"
    ),
    MealPhase.MID: (
        "当前是【餐中】：用户正在吃。"
        "这一阶段围绕「慢下来、觉察饱足」给**一句**具体可执行的提醒"
        "（例如放下筷子、多嚼几下再咽、尝一口描述味道、停下来感受七八分饱）。"
        "一次只说一件事，不要连问多个问题，不要重复上一轮说过的提醒。"
    ),
    MealPhase.POST: (
        "当前是【餐后】：这一餐已经结束。"
        "先肯定他完成了这顿饭，再邀请他说一句感受（饱足度、味道、有没有情绪性进食），"
        "最后给一句温和的收束。**不要再提「继续吃」「慢慢吃」「慢慢嚼」这类还在吃的引导**，"
        "也不要再核对咀嚼速度 —— 饭已经吃完了。"
    ),
    MealPhase.IDLE: "",
}

# ---- 意图识别（中文关键词；都要求句子较短，避免长句里偶然出现被误判）----
_START_WORDS = ("我要吃饭", "我吃饭了", "要吃饭", "开饭", "开始吃饭", "准备吃饭",
                "我准备吃", "该吃饭", "吃饭吧", "我吃点东西", "我开始吃")
_EATING_WORDS = ("开始吃", "开吃", "开动了", "动筷", "吃上了", "我在吃", "我吃了")
_FINISH_WORDS = ("吃完了", "吃好了", "吃饱了", "不吃了", "吃撑了", "结束用餐",
                 "吃完了谢谢", "我吃好了", "吃不下了", "不用了我不吃")

_NEG = ("没", "还没", "未", "没有", "不想", "不饿")


def _clean(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~：:;；\-_/\\]+", "", str(text or ""))


def _has_negation_before(text: str, word: str) -> bool:
    """词前有没有否定词（"我还没吃完"不能被当成吃完了）。"""
    idx = text.find(word)
    if idx < 0:
        return False
    before = text[max(0, idx - 3):idx]
    return any(n in before for n in _NEG)


def wants_start_meal(text: str) -> bool:
    t = _clean(text)
    if not t or len(t) > 14:
        return False
    return any(w in t for w in _START_WORDS if not _has_negation_before(t, w))


def wants_begin_eating(text: str) -> bool:
    t = _clean(text)
    if not t or len(t) > 14:
        return False
    return any(w in t for w in _EATING_WORDS if not _has_negation_before(t, w))


def wants_finish_meal(text: str) -> bool:
    """用户是否表达了"吃完了/不吃了"（带否定词保护）。"""
    t = _clean(text)
    if not t or len(t) > 20:
        return False
    return any(w in t for w in _FINISH_WORDS if not _has_negation_before(t, w))


@dataclass
class MealSession:
    """一餐的运行时状态。"""
    user_id: str = "default"
    started_at: float = field(default_factory=time.time)
    phase: MealPhase = MealPhase.PRE
    started_by: str = "user"                      # user（说要吃饭）/ vision（看到已经在吃）
    began_eating_at: Optional[float] = None      # 进入餐中的时刻
    finished_at: Optional[float] = None
    turns: int = 0                                # 这一餐里的对话轮数
    last_activity: float = field(default_factory=time.time)
    eating_seen_since: Optional[float] = None     # 摄像头连续看到"在吃"的起点
    idle_since: Optional[float] = None            # 连续"没在吃"的起点
    idle_asked_at: float = 0.0                    # 上次问"是不是吃完了"的时间
    recorded: bool = False                        # 统计幂等标志
    end_reason: str = ""

    @property
    def phase_zh(self) -> str:
        return PHASE_ZH.get(self.phase, "")

    @property
    def duration_min(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.started_at) / 60, 1)

    def guide(self) -> str:
        return PHASE_GUIDE.get(self.phase, "")


# 自动推进的判定阈值
AUTO_EAT_SUSTAIN_SEC = 25.0     # 摄像头连续看到进食多久 → 判为"已经在吃"（自动开餐/进餐中）
AUTO_SILENT_SEC = 10 * 60.0     # 连续这么久既没在吃也没说话 → 认定人已离开，收尾统计
AUTO_CLOSE_SEC = 45 * 60.0      # 一餐最多挂多久（超时强制收尾并统计）


class MealManager:
    """按 user_id 维护一餐的生命周期（进程内单例）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, MealSession] = {}
        # 还没有会话时，"摄像头似乎在吃"的起点：连续够久才真的开一餐，
        # 免得筷子晃一下、人在镜头前站一会儿就凭空多出一餐（用餐次数虚高）。
        self._watching: Dict[str, float] = {}

    # ---------- 查询 ----------
    def current(self, user_id: str = "default") -> Optional[MealSession]:
        with self._lock:
            return self._sessions.get(user_id)

    def is_active(self, user_id: str = "default") -> bool:
        return self.current(user_id) is not None

    def state(self, user_id: str = "default") -> Dict[str, Any]:
        s = self.current(user_id)
        if s is None:
            return {"active": False, "phase": MealPhase.IDLE.value, "phase_zh": ""}
        return {
            "active": True,
            "phase": s.phase.value,
            "phase_zh": s.phase_zh,
            "started_by": s.started_by,
            "started_at": s.started_at,
            "duration_min": s.duration_min,
            "turns": s.turns,
        }

    # ---------- 状态迁移 ----------
    def start(self, user_id: str = "default", *, force: bool = False,
              started_by: str = "user") -> MealSession:
        """开始一餐（已经在吃的话默认沿用，不重开）。"""
        with self._lock:
            cur = self._sessions.get(user_id)
            if cur is not None and not force:
                return cur
            sess = MealSession(user_id=user_id, started_by=started_by)
            self._sessions[user_id] = sess
            return sess

    def note_turn(self, user_id: str = "default") -> Optional[MealSession]:
        with self._lock:
            s = self._sessions.get(user_id)
            if s is not None:
                s.turns += 1
                s.last_activity = time.time()
            return s

    def to_mid(self, user_id: str = "default", *, reason: str = "user") -> Optional[MealSession]:
        """餐前 → 餐中。"""
        with self._lock:
            s = self._sessions.get(user_id)
            if s is None or s.phase is not MealPhase.PRE:
                return s
            s.phase = MealPhase.MID
            s.began_eating_at = time.time()
            s.last_activity = time.time()
            s.eating_seen_since = None
            print(f"[meal] 进入餐中（{reason}）", flush=True)
            return s

    def to_post(self, user_id: str = "default", *, reason: str = "user") -> Optional[MealSession]:
        """餐中 → 餐后。"""
        with self._lock:
            s = self._sessions.get(user_id)
            if s is None or s.phase is MealPhase.POST:
                return s
            s.phase = MealPhase.POST
            s.finished_at = time.time()
            s.last_activity = time.time()
            s.end_reason = reason
            print(f"[meal] 进入餐后（{reason}）", flush=True)
            return s

    def finish(self, user_id: str = "default", *, reason: str = "user") -> Optional[MealSession]:
        """收尾这一餐。**幂等**：只有第一次调用返回会话（调用方据此统计一次）。

        返回 None 表示"没有需要收尾的会话，或者已经收尾过了" ——
        调用方必须据此决定要不要落库，这样重复触发也不会重复统计。
        """
        with self._lock:
            s = self._sessions.pop(user_id, None)
        if s is None:
            return None
        s.phase = MealPhase.POST
        s.finished_at = s.finished_at or time.time()
        s.end_reason = reason
        if s.recorded:
            return None
        s.recorded = True
        return s

    def note_metrics(self, user_id: str, metrics: Dict[str, Any]) -> Optional[str]:
        """把摄像头指标喂进来，返回**建议的动作**（None / "ask_finished" / "to_mid"）。

        只做判定与建议，不直接开口说话 —— 说话统一由 autonomy 决定，
        避免这里和自主互动各说一句互相打岔。

        另外：**没有会话时也要看**。摄像头连续看到进食就自动开一餐（直接落到餐中），
        否则"没人喊开饭就不存在这一餐"，用餐次数永远统计不上。
        """
        now = time.time()
        eating = self._looks_like_eating(metrics)
        with self._lock:
            s = self._sessions.get(user_id)
            if s is None:
                if not eating:
                    self._watching.pop(user_id, None)
                    return None
                first = self._watching.setdefault(user_id, now)
                if now - first < AUTO_EAT_SUSTAIN_SEC:
                    return None
                # 看到的是"已经在吃"：直接开在餐中，别假装还需要餐前引导
                self._watching.pop(user_id, None)
                self.start(user_id, force=True, started_by="vision")
                self.to_mid(user_id, reason="vision")
                return None
            if eating:
                s.idle_since = None
                if s.phase is MealPhase.PRE:
                    if s.eating_seen_since is None:
                        s.eating_seen_since = now
                    elif now - s.eating_seen_since >= AUTO_EAT_SUSTAIN_SEC:
                        # 锁是可重入的，to_mid 里再次 acquire 没问题
                        self.to_mid(user_id, reason="vision")
                        return None
                else:
                    s.eating_seen_since = None
                s.last_activity = now
            else:
                s.eating_seen_since = None
                # 餐中"停下来了" → 建议问一句"是不是吃完了"。
                # 只在这里判（**唯一一处**）：以前 autonomy 里还有一套同样的计时，
                # 两边各自计时、各自冷却，结果用户停一会儿就被问一次、反复追问。
                # 要求摄像头开着且桌边确实有人 —— 人都不在画面里就别问"吃完了吗"。
                if not (s.phase is MealPhase.MID and metrics.get("vision", True)
                        and (metrics.get("face_count") or 0)):
                    s.idle_since = None
                    return None
                cfg = ic.EATING_IDLE
                if not cfg.get("enabled"):
                    return None
                if s.idle_since is None:
                    s.idle_since = now
                elif now - s.idle_since >= float(cfg["sustain_sec"]):
                    if now - s.idle_asked_at < float(cfg["cooldown_sec"]):
                        return None                 # 刚问过，别再追问
                    s.idle_since = now              # 重新计时
                    s.idle_asked_at = now
                    return "ask_finished"
            return None

    @staticmethod
    def _looks_like_eating(metrics: Dict[str, Any]) -> bool:
        """是否"看起来在吃"：有咀嚼或有送食，且画面里确实有人。"""
        if not metrics:
            return False
        if not (metrics.get("face_count") or 0):
            return False
        try:
            cpm = float(metrics.get("chews_per_min") or 0)
        except (TypeError, ValueError):
            cpm = 0.0
        try:
            bpm = float(metrics.get("bites_per_min") or 0)
        except (TypeError, ValueError):
            bpm = 0.0
        return cpm >= 5.0 or bpm >= 1.0

    def expire_reason(self, user_id: str = "default") -> str:
        """该强制收尾的原因（"" = 还不用收）。

        * `auto_silent`：连续 `AUTO_SILENT_SEC`（10 分钟）**既没在吃也没说话**
          —— 管"用户直接走了""问了他也不回应"这两种情况（`last_activity`
          只在检测到进食或有一次对话轮次时刷新）。
        * `auto_timeout`：整餐挂过 `AUTO_CLOSE_SEC`（45 分钟）的兜底。
        """
        s = self.current(user_id)
        if s is None:
            return ""
        now = time.time()
        if now - s.last_activity > AUTO_SILENT_SEC:
            return "auto_silent"
        if now - s.started_at > AUTO_CLOSE_SEC:
            return "auto_timeout"
        return ""

    def expired(self, user_id: str = "default") -> bool:
        """这一餐是否该强制收尾了（详见 expire_reason）。"""
        return bool(self.expire_reason(user_id))

    def reset(self, user_id: str = "default") -> None:
        with self._lock:
            self._sessions.pop(user_id, None)
            self._watching.pop(user_id, None)


meal_manager = MealManager()
