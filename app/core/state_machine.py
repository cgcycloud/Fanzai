"""正念进食对话状态机 —— 餐前 6 步 → 餐中 5 步 → 餐后 3 步 → 结束。

改进点（相对旧版内置）：
  - 空消息不推进状态（只重复当前提示语）
  - 结束关键词（结束/谢谢/可以了/再见）任意状态可退出
  - summary_dict 供一餐总结与数据落库
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DialogueState(Enum):
    IDLE = "idle"
    PRE_MEAL_BREATH = "pre_meal_breath"
    PRE_MEAL_HUNGER = "pre_meal_hunger"
    PRE_MEAL_EMOTION = "pre_meal_emotion"
    PRE_MEAL_MOTIVATION = "pre_meal_motivation"
    PRE_MEAL_HEALTH_CHECK = "pre_meal_health_check"
    PRE_MEAL_FEEDBACK = "pre_meal_feedback"
    MID_MEAL_HUNGER = "mid_meal_hunger"
    MID_MEAL_ENJOYMENT = "mid_meal_enjoyment"
    MID_MEAL_SPEED = "mid_meal_speed"
    MID_MEAL_FULLNESS = "mid_meal_fullness"
    MID_MEAL_FEEDBACK = "mid_meal_feedback"
    POST_MEAL_IMMEDIATE = "post_meal_immediate"
    POST_MEAL_LONG_TERM = "post_meal_long_term"
    POST_MEAL_SUMMARY = "post_meal_summary"
    END = "end"


PROMPTS: Dict[DialogueState, str] = {
    DialogueState.PRE_MEAL_BREATH: "让我们先做三次深呼吸，放松一下。",
    DialogueState.PRE_MEAL_HUNGER: "你现在真的感到饥饿吗？请描述一下。",
    DialogueState.PRE_MEAL_EMOTION: "你现在是真的感到饥饿，还是因为压力大、孤独或想要庆祝？",
    DialogueState.PRE_MEAL_MOTIVATION: "你确定现在是进食的好时机吗？",
    DialogueState.PRE_MEAL_HEALTH_CHECK: "你有没有需要特别注意的健康状况？比如糖尿病、高血压？",
    DialogueState.PRE_MEAL_FEEDBACK: "好的，开始用餐吧。记得慢慢吃，感受每一口食物。",
    DialogueState.MID_MEAL_HUNGER: "现在感觉饥饿程度如何？",
    DialogueState.MID_MEAL_ENJOYMENT: "你享受现在的食物吗？",
    DialogueState.MID_MEAL_SPEED: "你的进食速度如何？",
    DialogueState.MID_MEAL_FULLNESS: "感觉几分饱了？",
    DialogueState.MID_MEAL_FEEDBACK: "继续用餐，注意感受。",
    DialogueState.POST_MEAL_IMMEDIATE: "餐后感觉如何？",
    DialogueState.POST_MEAL_LONG_TERM: "这顿饭对你的影响如何？",
    DialogueState.POST_MEAL_SUMMARY: "这顿饭整体不错！总结一下今天的饮食情况。",
}

TRANSITIONS: Dict[DialogueState, DialogueState] = {
    DialogueState.PRE_MEAL_BREATH: DialogueState.PRE_MEAL_HUNGER,
    DialogueState.PRE_MEAL_HUNGER: DialogueState.PRE_MEAL_EMOTION,
    DialogueState.PRE_MEAL_EMOTION: DialogueState.PRE_MEAL_MOTIVATION,
    DialogueState.PRE_MEAL_MOTIVATION: DialogueState.PRE_MEAL_HEALTH_CHECK,
    DialogueState.PRE_MEAL_HEALTH_CHECK: DialogueState.PRE_MEAL_FEEDBACK,
    DialogueState.PRE_MEAL_FEEDBACK: DialogueState.MID_MEAL_HUNGER,
    DialogueState.MID_MEAL_HUNGER: DialogueState.MID_MEAL_ENJOYMENT,
    DialogueState.MID_MEAL_ENJOYMENT: DialogueState.MID_MEAL_SPEED,
    DialogueState.MID_MEAL_SPEED: DialogueState.MID_MEAL_FULLNESS,
    DialogueState.MID_MEAL_FULLNESS: DialogueState.MID_MEAL_FEEDBACK,
    DialogueState.MID_MEAL_FEEDBACK: DialogueState.POST_MEAL_IMMEDIATE,
    DialogueState.POST_MEAL_IMMEDIATE: DialogueState.POST_MEAL_LONG_TERM,
    DialogueState.POST_MEAL_LONG_TERM: DialogueState.POST_MEAL_SUMMARY,
    DialogueState.POST_MEAL_SUMMARY: DialogueState.END,
}

END_KEYWORDS = ("结束", "谢谢", "可以了", "再见", "吃完", "吃好", "吃饱了", "不吃了")
# 否定词：避免"还没吃完""没吃完"被当成"已吃完"
_NEGATIONS = ("没", "还没", "未", "没有", "不想")


def said_finished(text: str) -> bool:
    """用户是否表达了"吃完了/不吃了"（带否定词保护）。"""
    for kw in END_KEYWORDS:
        idx = text.find(kw)
        if idx < 0:
            continue
        before = text[max(0, idx - 3):idx]
        if any(neg in before for neg in _NEGATIONS):
            continue
        return True
    return False
IDLE_PROMPT = "你好，我是正念饭崽。点击「开始一餐」后，我会陪你完成餐前-餐中-餐后的正念进食流程。"

# 阶段归类（供事件落库 / 记忆检索）
PHASE_OF_STATE: Dict[DialogueState, str] = {}
for _s in list(DialogueState):
    if _s.value.startswith("pre_meal"):
        PHASE_OF_STATE[_s] = "pre_meal"
    elif _s.value.startswith("mid_meal"):
        PHASE_OF_STATE[_s] = "mid_meal"
    elif _s.value.startswith("post_meal"):
        PHASE_OF_STATE[_s] = "post_meal"
    else:
        PHASE_OF_STATE[_s] = "idle"


@dataclass
class DialogueStateMachine:
    state: DialogueState = DialogueState.IDLE
    history: List[Dict[str, Any]] = field(default_factory=list)
    summary_data: Dict[str, Any] = field(default_factory=dict)

    def start_full_meal_session(self) -> None:
        self.state = DialogueState.PRE_MEAL_BREATH
        self.history = []
        self.summary_data = {}

    def current_prompt(self) -> str:
        if self.state is DialogueState.IDLE:
            return IDLE_PROMPT
        if self.state is DialogueState.END:
            return self._generate_summary()
        return PROMPTS.get(self.state, IDLE_PROMPT)

    @property
    def phase(self) -> str:
        return PHASE_OF_STATE.get(self.state, "idle")

    def process_user_input(self, user_text: str, voice_features: Optional[Dict] = None) -> tuple[str, bool]:
        """处理用户输入，返回 (回复, 是否结束)。空消息只重复当前提示，不推进。"""
        if not (user_text or "").strip():
            return self.current_prompt(), self.state is DialogueState.END

        self.history.append({"user": user_text, "features": voice_features or {}})

        if said_finished(user_text):
            self.state = DialogueState.END
            return "好的，再见。祝你用餐愉快！", True

        next_state = TRANSITIONS.get(self.state, DialogueState.END)
        self.state = next_state
        if next_state is DialogueState.END:
            return self._generate_summary(), True
        return self.current_prompt(), False

    def _generate_summary(self) -> str:
        return (
            f"今天的正念进食结束了：共交流 {len(self.history)} 轮。"
            "记住，吃饭时要慢一点，感受食物的味道和身体的感觉。"
        )

    def summary_dict(self) -> Dict[str, Any]:
        """一餐总结（供 meal_summary 事件落库）。"""
        return {
            "eating_type": self.summary_data.get("eating_type", "生理性进食"),
            "trigger_emotion": self.summary_data.get("trigger_emotion", "无"),
            "eating_state": self.summary_data.get("eating_state", "正常"),
            "intervention_strategy": self.summary_data.get("intervention_strategy", "保持正念"),
            "total_turns": len(self.history),
        }

    def reset(self) -> None:
        self.state = DialogueState.IDLE
        self.history = []
        self.summary_data = {}
