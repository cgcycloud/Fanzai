"""对话 API —— SSE 流式（分句推送 + TTS 有序队列）+ 非流式 + 会话管理。

状态机空消息不推进；结束关键词任意状态可退出；流式不输出 [EMOTION:xx]
标签（防拆包泄漏），情感由回复关键词兜底判断。
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import config
from ..core.ai_client import LocalEchoClient, get_ai_client
from ..core.db import HealthStore, utc_now
from ..core import interaction_config as ic
from ..core.memory import ConversationMemory
from ..vision.service import vision_service
from ..voice import tts_cache
from ..voice.tts import cache_voice as tts_voice_key
from ..voice.tts import detect_format, synthesize

router = APIRouter(prefix="/api/dialogue")

MIN_CHUNK_CHARS = 6      # 小于这个长度的片段不单独成句（避免"嗯，"这类碎片音频）

VALID_EMOTION = {"happy", "sad", "angry", "surprise", "neutral",
                 "love", "sleepy", "cool", "cry", "wink", "shy"}

EMOTION_KEYWORDS = {
    "happy": ["开心", "高兴", "愉快", "喜悦", "棒", "好", "不错", "幸福", "微笑", "哈哈", "嘻嘻", "耶", "太棒了", "完美", "乐", "笑", "美好", "真好", "喜欢"],
    "sad": ["难过", "伤心", "失落", "沮丧", "抑郁", "无助", "痛苦", "遗憾", "叹气", "唉", "沉重", "悲伤", "心碎", "委屈", "忧郁"],
    "angry": ["生气", "愤怒", "恼火", "烦躁", "不满", "讨厌", "恼", "怒", "暴躁", "火大", "烦"],
    "surprise": ["惊讶", "惊喜", "意外", "吃惊", "天哪", "哇", "竟然", "居然", "天啊", "真的吗", "想不到", "神奇"],
    "love": ["爱", "温暖", "关怀", "拥抱", "宝贝", "甜蜜", "心动", "喜欢", "亲爱的", "感动", "美好", "温柔", "呵护", "关心"],
    "sleepy": ["困", "累", "疲惫", "瞌睡", "休息", "睡觉", "乏力", "疲倦", "疲劳", "歇"],
    "cool": ["酷", "帅", "厉害", "优秀", "漂亮", "棒极了", "了不起", "太强了", "非常棒"],
    "cry": ["哭", "泪", "流泪", "哽咽", "心碎", "哭泣", "嚎啕", "痛哭", "想哭"],
    "wink": ["调皮", "俏皮", "挤眼", "机灵", "鬼马", "得意", "调皮鬼", "古灵精怪"],
    "shy": ["害羞", "腼腆", "羞涩", "不好意思", "脸红", "难为情", "含蓄", "温和", "内向"],
}
NEGATIONS = ["不", "别", "没", "不是", "不要", "不会", "不必", "别说", "别想"]
COMFORT_PHRASES = ["别难过", "别伤心", "不要难过", "不要伤心", "别生气", "不要生气",
                   "别沮丧", "别低落"]


def extract_emotion_from_text(text: str | None) -> str:
    """从回复文本推断情感标签（驱动机器人表情）。"""
    if not text:
        return "neutral"
    for phrase in COMFORT_PHRASES:
        if phrase in text:
            return "love"
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            idx = text.find(kw)
            if idx < 0:
                continue
            before = text[max(0, idx - 3):idx]
            if not any(neg in before for neg in NEGATIONS):
                return emotion
    return "neutral"


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句（保留标点），用于逐句 TTS。"""
    parts = re.split(r"(?<=[。！？；!?;])", text)
    return [p.strip() for p in parts if p and p.strip()]


def _sse(event: str, data: dict) -> str:
    """构造一条 SSE 消息。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 未配置真实 AI 时的提示（不是本地"假回复"，只是明确告知，避免用规则话术冒充 AI）
AI_UNCONFIGURED_NOTICE = (
    "我还没接入 AI 模型，暂时没法好好陪你聊。"
    "请在 data_local/ai_config.json 里填入 API 地址和 Key 后再试。"
)
AI_EMPTY_NOTICE = "我刚刚没接上话，能再说一次吗？"


def _ai_opening(phase: str, meal_active: bool = False) -> str:
    """开场白也交给 AI 生成，不朗读本地脚本。"""
    client = get_ai_client()
    if isinstance(client, LocalEchoClient):
        return AI_UNCONFIGURED_NOTICE
    try:
        reply = (client.chat(_build_llm_prompt("", phase, meal_active)) or "").strip()
    except Exception as exc:
        return f"AI 暂时不可用：{exc}"
    return reply or AI_EMPTY_NOTICE


@dataclass
class DialogueSession:
    """一次会话的运行期容器（只留诊断用的对话流水）。

    这里**不再持有按轮数推进的 `DialogueStateMachine`**：那一套每收一条消息就前进一步，
    13 轮后自动判定"结束用餐"并落一次统计，跟真实吃没吃完毫无关系（"谢谢""结束"
    这类日常用词也会把一餐收掉）—— 正是"吃饭前后逻辑混乱"的来源。
    一餐的阶段只由 `core/meal.py` 的 `meal_manager` 按真实信号维护。
    """
    turns: list = field(default_factory=list)

    def note(self, user_text: str, phase: str, finished: bool) -> None:
        """记一轮（只留最近 50 条，供诊断看流水；设备是长跑的，不能无限攒）。"""
        self.turns.append({"user": user_text, "phase": phase, "finished": finished})
        del self.turns[:-50]


_DIALOGUE_LOCK = threading.Lock()
_SESSIONS: dict[str, DialogueSession] = {}


def _get_session(user_id: str) -> DialogueSession:
    with _DIALOGUE_LOCK:
        sess = _SESSIONS.get(user_id)
        if sess is None:
            sess = DialogueSession()
            _SESSIONS[user_id] = sess
        return sess


def _pop_session(user_id: str) -> None:
    with _DIALOGUE_LOCK:
        _SESSIONS.pop(user_id, None)


def _set_robot_emotion(emotion: str) -> None:
    if emotion in VALID_EMOTION:
        vision_service.set_robot_emotion(emotion)
    # 设备小屏：情感表情 + 指标里的情绪
    try:
        from ..display.service import screen_service
        screen_service.on_emotion(emotion)
    except Exception:
        pass


def _screen_thinking() -> None:
    """开始生成回复 → 设备屏切到「思考」表情。"""
    try:
        from ..display.service import screen_service
        screen_service.on_thinking()
    except Exception:
        pass


def _screen_speaking(est_seconds: float = 4.0) -> None:
    """有音频要播 → 设备屏切到「说话」表情（按音频时长估算持续秒数）。"""
    try:
        from ..display.service import screen_service
        screen_service.on_speaking(est_seconds)
    except Exception:
        pass


def _screen_idle() -> None:
    try:
        from ..display.service import screen_service
        screen_service.on_idle()
    except Exception:
        pass


def _screen_turn(user_text: str, reply: str) -> None:
    """把这一轮对话推给设备屏（只保留最近两轮）。"""
    try:
        from ..display.service import screen_service
        screen_service.on_turn(user_text, reply)
    except Exception:
        pass


def _log_emotion_event(store: HealthStore, user_id: str, emotion: str, reply: str) -> None:
    try:
        store.add_event("emotion", {"user_id": user_id, "emotion": emotion,
                                    "reply_snippet": reply[:120]}, status="local")
    except Exception as exc:
        print(f"emotion event write warn: {exc}")


def _log_dialogue_event(store: HealthStore, user_id: str, user_text: str,
                        reply: str, phase: str) -> None:
    """写入 dialogue 事件：周报的"对话轮次"、记忆检索与情绪趋势都依赖它。"""
    try:
        store.add_event("dialogue", {"user_id": user_id, "user_text": user_text,
                                     "assistant_reply": reply, "phase": phase},
                        status="local")
    except Exception as exc:
        print(f"dialogue event write warn: {exc}")


def _screen_metrics() -> dict:
    """取设备屏当前的实时指标（咀嚼/进食/情绪），失败返回空。"""
    try:
        from ..display.state import display_state
        return display_state.metrics()
    except Exception:
        return {}


def _confirmed_by_answer(text: str) -> bool:
    """机器人刚问过"是不是吃完了"，用户只回一句"对/嗯/是的"也算吃完。

    仅在"确实刚问过那个问题"（3 分钟内）时才生效，避免把普通对话里的
    "对""是"误判成结束用餐。
    """
    try:
        from ..core.autonomy import autonomy_service
        if not autonomy_service.asked_finished_recently():
            return False
    except Exception:
        return False
    t = "".join(ch for ch in (text or "") if ch not in " \t\n，。！？、,.!?~")
    if not t or len(t) > 8:
        return False
    if any(neg in t for neg in ("没", "不", "别", "还没")):
        return False
    return any(kw in t for kw in ("对", "是", "嗯", "好的", "吃完", "饱了"))


def _close_meal(store: HealthStore, user_id: str, *, reason: str,
                turns=None, phase: str = "") -> bool:
    """收尾一餐并**只统计一次**。

    返回 True 表示这次调用真的落了库（调用方可据此提示）。
    统计两件事：
      * `food_residual` —— 健康分析算「用餐次数/平均间隔/依从性」用的就是它；
      * `meal_summary`  —— 一餐总结（轮数、进食类型），周报里单独统计。

    幂等由 meal_manager.finish() 保证：它 pop 掉会话并检查 recorded 标志，
    只有第一次返回非 None，所以重复触发不会重复计数。
    """
    from ..core.meal import meal_manager

    sess_meal = meal_manager.finish(user_id, reason=reason)
    if sess_meal is None:
        return False

    _log_meal_record(store, user_id, session=sess_meal, turns=turns,
                     phase=phase or sess_meal.phase.value, reason=reason)
    try:
        store.add_event("meal_summary", {
            "user_id": user_id,
            "total_turns": sess_meal.turns,
            "duration_min": sess_meal.duration_min,
            "end_reason": reason,
            "started_ts": sess_meal.started_at,
            "trigger_ts": sess_meal.finished_at or time.time(),
        }, status="local")
    except Exception as exc:
        print(f"[meal] 写 meal_summary 失败: {exc}", flush=True)
    print(f"[meal] 一餐已收尾并统计一次（{reason}，{sess_meal.duration_min} 分钟，"
          f"{sess_meal.turns} 轮）", flush=True)
    # 这一餐结束了 → 自动切回日常聊天模式（摄像头关掉；用户下次说「我要吃饭」再切回来）
    _auto_switch_mode(ic.MODE_CHAT, "用餐结束")
    return True


def _log_meal_record(store: HealthStore, user_id: str, *, session=None,
                     turns=None, phase: str = "", reason: str = "user_said_finished") -> None:
    """一餐结束 → 记一条 food_residual，并带上这一餐前面的信息。

    健康分析里的「用餐次数」「平均用餐间隔」「依从性评分」都依赖这个事件；
    同时把这一餐观察到的咀嚼次数/频率、进食速度、情绪与对话片段一起留存，
    方便以后回看"这顿吃得怎么样"。
    """
    now = time.time()
    metrics = _screen_metrics()
    payload: dict = {
        "user_id": user_id,
        "trigger_ts": now,
        "meal_finished": True,
        "source": reason,
        "phase": phase,
    }
    if session is not None:
        # MealSession 用 started_at；旧调用方可能传 DialogueSession（meal_started_at）
        started = (getattr(session, "started_at", None)
                   or getattr(session, "meal_started_at", None))
        if started:
            payload["started_ts"] = started
            payload["duration_min"] = round((now - started) / 60, 1)
        turns_in_meal = getattr(session, "turns", None)
        if turns_in_meal:
            payload["meal_turns"] = turns_in_meal
    if metrics:
        payload.update({
            "chew_count": metrics.get("chew_count"),
            "chews_per_min": metrics.get("chews_per_min"),
            "chew_level": metrics.get("chew_level"),
            "bites_per_min": metrics.get("bites_per_min"),
            "eat_level": metrics.get("eat_level"),
            "emotion": metrics.get("emotion"),
            "face_count": metrics.get("face_count"),
        })
    if turns:
        payload["turns"] = [{"user": (t.get("user_text") or t.get("user") or ""),
                             "assistant": (t.get("assistant_reply") or t.get("assistant") or "")}
                            for t in turns[-10:]]
    try:
        store.add_event("food_residual", payload, status="local")
    except Exception as exc:
        print(f"meal record warn: {exc}")


def _clear_finished_question() -> None:
    """已经据此记过一次进食，清掉标记，避免同一句"对"重复计数。"""
    try:
        from ..core.autonomy import autonomy_service
        autonomy_service.clear_finished_question()
    except Exception:
        pass


def _auto_switch_mode(mode: str, why: str) -> None:
    """用餐开始/结束时自动切运行模式（**保留用户选的自主互动级别**）。

    用户要的交互是：
      * 日常聊天模式里说「我要吃饭」→ 自动切到**进食检测**（摄像头开，开始看咀嚼/食物）；
      * 进食检测模式里这一餐结束 → 自动切回**日常聊天**（摄像头关，省电省算力）。
    切模式要顺带把硬件落实（开/关摄像头、下发音量），所以调 agent.apply_settings()。
    """
    try:
        cur = ic.get_settings()
        target = ic.MODES.get(mode, {}).get("name", mode)
        if cur.mode == mode:
            return
        settings = ic.switch_mode(mode)
        from .agent import apply_settings          # 延迟导入：避免 api 模块间循环依赖
        apply_settings(settings)
        print(f"[mode] {why} → 自动切换到「{target}」（自主互动保持 {settings.autonomy_name}）",
              flush=True)
    except Exception as exc:
        print(f"[mode] 自动切换失败: {exc}", flush=True)


def _compress_memory_soon(memory, client) -> None:
    """这一轮之后把超窗的旧对话压成摘要（**不占用这一轮的响应时间**）。

    为什么放后台线程：压缩要打一次模型（几秒），放在回复前会拖慢首句、
    放在 `done` 之前会拖住"回到待聆听"，而它跟用户这一轮的回复无关。
    没配真实 AI 时跳过 —— 本地规则客户端的输出会把摘要污染成"我在听…"。
    """
    try:
        if isinstance(client, LocalEchoClient) or memory.token_count() <= memory.usable_tokens:
            return
    except Exception:
        return

    def _run() -> None:
        try:
            memory.compress_if_needed(client)
        except Exception as exc:
            print(f"[memory] 压缩跳过: {exc}", flush=True)

    threading.Thread(target=_run, name="memory-compress", daemon=True).start()


def _advance_meal(user_id: str, message: str) -> tuple[str, bool, str]:
    """把这一句话喂给"这一餐"，返回 (当前阶段, 这一句之后是否收尾统计, 收尾原因)。

    规则只有这几条（完整说明见 core/meal.py 头部）：

      * 不在用餐中 + 说「我要吃饭/开饭了」          → 开一餐（餐前）
      * 餐前      + 说「开吃/开始吃了」             → 餐中
      * 任何阶段  + 说「吃完了/不吃了」             → 餐后（这一句回复完就收尾统计）
      * 刚被问过「是不是吃完了」+ 回「对/嗯/是的」  → 同上

    **不再按对话轮数推进**：旧状态机每收一条消息就前进一步，13 轮后自动"结束用餐"
    并落一次统计，于是"谢谢""结束"这类日常用词会莫名其妙把一餐收掉。
    """
    from ..core.meal import (MealPhase, meal_manager, wants_begin_eating,
                             wants_finish_meal, wants_start_meal)

    if not meal_manager.is_active(user_id) and wants_start_meal(message):
        meal_manager.start(user_id)
        # 「我要吃饭」= 要开始监测这一餐 → 自动切到进食检测模式（摄像头开）
        _auto_switch_mode(ic.MODE_MEAL, "用户说要吃饭了")
    if meal_manager.is_active(user_id):
        if wants_begin_eating(message):
            meal_manager.to_mid(user_id, reason="user")
        if wants_finish_meal(message):
            meal_manager.to_post(user_id, reason="user_said_finished")
        elif _confirmed_by_answer(message):
            # 刚问过"是不是吃完了"，回一句"对/嗯"也算（没问过时 _confirmed_by_answer 为假）
            meal_manager.to_post(user_id, reason="confirmed_by_answer")
        meal_manager.note_turn(user_id)

    meal = meal_manager.current(user_id)
    if meal is None:
        return "idle", False, ""
    finished = meal.phase is MealPhase.POST     # 餐后=收尾阶段，回复完即统计
    return meal.phase.value, finished, (meal.end_reason if finished else "")


def _build_llm_prompt(user_msg: str, phase: str, meal_active: bool = False,
                      last_reply: str = "") -> str:
    """构造本轮提示：角色由运行模式预设决定，不塞任何本地话术。

    三个防"模板腔/答非所问"的关键点：
      - **只有真正在一餐里**才注入用餐阶段，且注入的是该阶段的**行为要求**
        （见 core/meal.py 的 PHASE_GUIDE），不是一句泛泛的"当前处于餐前" ——
        旧版只说了阶段名，模型不知道该干什么，于是餐前就在谈咀嚼速度；
      - 普通聊天不会被硬拉回"你饿了吗"的固定套路；
      - 带上上一轮的回答，明确要求换一种说法。
    """
    role_prompt = ""
    try:
        from ..core.interaction_config import get_settings
        role_prompt = get_settings().mode_info.get("role_prompt", "")
    except Exception:
        pass
    parts = [role_prompt] if role_prompt else []
    if meal_active:
        guide = _phase_guide(phase)
        if guide:
            parts.append(guide)
    if user_msg:
        parts.append(f"用户说：{user_msg}")
        parts.append("先回应用户说的这件事本身，再自然延展；"
                     "用温暖自然的中文，1~3 句、总字数不超过 80 字。")
    else:
        parts.append("请主动打招呼；如果用户正在用餐就顺势做一句引导，"
                     "否则就自然聊两句。1~2 句，总字数不超过 50 字。")
    if last_reply:
        parts.append(f"你上一轮已经说过：「{last_reply[:60]}」。"
                     "这一轮换一种说法，不要重复同样的开头或句式。")
    return "\n".join(parts)


def _phase_guide(phase: str) -> str:
    """把阶段名映射成该阶段的行为要求。"""
    try:
        from ..core.meal import PHASE_GUIDE, MealPhase
        for p in MealPhase:
            if p.value == phase:
                return PHASE_GUIDE.get(p, "")
    except Exception:
        pass
    return ""


@router.post("/stream")
async def dialogue_stream(request: Request):
    """SSE 流式对话：reply（分句文本）/ audio（逐句 TTS base64）/ done 事件。"""
    body = await request.json()
    user_id = str(body.get("user_id") or "default")
    message = str(body.get("message") or "")
    reset = bool(body.get("reset"))

    memory = ConversationMemory.load(user_id)
    store = HealthStore()

    async def event_stream():
        # ---------- reset ----------
        if reset:
            _pop_session(user_id)
            memory.turns = []
            memory.summary = ""
            memory.save()
            _set_robot_emotion("neutral")
            yield _sse("reply", {"text": "已重置，开始新一轮正念进食引导。"})
            yield _sse("done", {"ok": True, "reset": True})
            return

        sess = _get_session(user_id)
        from ..core.meal import meal_manager
        _meal = meal_manager.current(user_id)
        phase, meal_active = (_meal.phase.value, True) if _meal else ("idle", False)
        last_reply = (memory.turns[-1].get("assistant_reply") if memory.turns else "") or ""

        # ---------- 空消息：只回当前引导语，不推进状态机 ----------
        if not message.strip():
            prompt = _ai_opening(phase, meal_active)   # 开场白由 AI 生成，不朗读本地脚本
            yield _sse("reply", {"text": prompt})
            yield _sse("done", {"phase": phase, "finished": False,
                                "memory_turns": len(memory.turns),
                                "emotion": extract_emotion_from_text(prompt)})
            return

        # ---- 一餐的生命周期：由真实信号驱动（见 core/meal.py 与 _advance_meal）----
        phase, finished, finish_reason = _advance_meal(user_id, message)
        meal_active = phase != "idle"

        sess.note(message, phase, finished)
        _screen_thinking()      # 设备屏：开始生成 → 思考表情

        # ================= 关键：所有阻塞工作放进后台线程 =================
        # async 生成器里若出现阻塞调用（requests 流式读取、TTS 合成、Event.wait），
        # 事件循环拿不回控制权，Starlette 的 send() 就无法真正刷出数据，
        # 前端会等到生成器全部结束才一次性收到（实测 12 秒后才出现第一句）。
        # 因此这里用「后台线程生产事件 → 队列 → 异步生成器消费」的结构。
        events: queue.Queue = queue.Queue()

        def emit(kind: str, payload: dict) -> None:
            events.put((kind, payload))

        def producer() -> None:
            full_reply_parts: list[str] = []
            pending = ""
            client = get_ai_client()
            # 未配置真实 AI：明确告知，不用本地规则话术冒充对话
            if isinstance(client, LocalEchoClient):
                emit("reply", {"text": AI_UNCONFIGURED_NOTICE})
                emit("done", {"phase": phase, "finished": False,
                              "emotion": "neutral", "unconfigured": True})
                events.put(None)
                return
            llm_prompt = _build_llm_prompt(message, phase, meal_active, last_reply)
            # 语音播报开关（下滑设置面板）：关掉就只出文字，**完全不打 TTS**
            # （省掉整条云端合成链路，回复的文本照样按句推给前端）
            speak = ic.tts_enabled()

            # ---- TTS：并行合成（单句 1~4 秒，串行会线性累积）+ 按句序发出 ----
            # 并行度实测：4 句 / 2 线程 2.7s，4 线程降到 1.6s —— 首句仍是关键路径，
            # 提高并行度是为了缩短"整段说完"的总时长。
            tts_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tts")
            tts_lock = threading.Condition()
            ready: dict[int, dict | None] = {}   # seq -> 音频事件（None 表示必须丢弃）
            tts_seq = 0                          # 已提交的句子数
            emit_seq = 0                         # 下一个待发出的句子序号

            def synth(seq: int, sentence: str) -> None:
                payload: dict | None
                try:
                    if not speak:
                        with tts_lock:
                            ready[seq] = {"text": sentence, "audio_b64": None}
                            tts_lock.notify_all()
                        return
                    voice = tts_voice_key(client)
                    cached = tts_cache.get(sentence, voice)      # 命中缓存直接秒发
                    if cached is not None:
                        payload = {"text": sentence, "format": detect_format(cached),
                                   "cached": True,
                                   "audio_b64": base64.b64encode(cached).decode("ascii")}
                    else:
                        out = (config.PATHS.media_dir / "audio"
                               / f"tts-{int(time.time() * 1000)}-{seq}.wav")
                        out.parent.mkdir(parents=True, exist_ok=True)
                        # 走统一 TTS 链（Qwen3-TTS-Flash；失败由上层降级为提示音）
                        if synthesize(sentence, out, voice=None):
                            raw = out.read_bytes()
                            tts_cache.put(sentence, voice, raw)
                            payload = {"text": sentence, "format": detect_format(raw),
                                       "audio_b64": base64.b64encode(raw).decode("ascii")}
                            out.unlink(missing_ok=True)
                        else:
                            payload = {"text": sentence, "audio_b64": None}
                except Exception:
                    payload = {"text": sentence, "audio_b64": None}
                with tts_lock:
                    ready[seq] = payload
                    tts_lock.notify_all()

            def submit_tts(sentence: str) -> None:
                nonlocal tts_seq
                tts_pool.submit(synth, tts_seq, sentence)
                tts_seq += 1

            def audio_emitter() -> None:
                """按句序发出音频：先合成完的先等着，保证播放顺序与文本一致。"""
                nonlocal emit_seq
                while True:
                    with tts_lock:
                        while emit_seq not in ready:
                            # wait 返回 False 才是真超时；被 notify 唤醒要继续等本序号
                            if not tts_lock.wait(timeout=config.TTS_QUEUE_TIMEOUT):
                                return
                        payload = ready.pop(emit_seq)
                        emit_seq += 1
                    if payload is None:      # 序列结束哨兵
                        return
                    if not speak and not payload.get("audio_b64"):
                        continue             # 语音播报关掉：连 audio 事件都不发（只出文字）
                    b64 = payload.get("audio_b64") or ""
                    est = max(1.5, len(b64) * 0.75 / 16000.0)   # 约 16KB/s
                    _screen_speaking(est)
                    emit("audio", payload)

            emitter = threading.Thread(target=audio_emitter, name="dialogue-audio", daemon=True)
            emitter.start()

            # 生成等待期不播任何填充语：旧版会念「让我想想」「嗯，我在听」，
            # 用户明确要求去掉 —— 等待由前端的 waiting 动画表达即可。
            # （唤醒应答「我在」是另一条链路，见 /api/agent/wake/ack，保持不变。）

            try:
                for delta in client.chat_stream(llm_prompt, context=memory.context_text() or None,
                                               user_text=message):
                    if not delta:
                        continue
                    pending += delta
                    full_reply_parts.append(delta)
                    parts = split_sentences(pending)
                    if len(parts) > 1 or (parts and re.search(r"[。！？；!?;]$", pending.strip())):
                        # 太短的片段（如"嗯，"）不单独送 TTS，否则听感破碎
                        flushable = len(parts) > 1 or len(parts[0]) >= MIN_CHUNK_CHARS
                        if flushable:
                            for s in parts:
                                emit("reply", {"text": s})
                                submit_tts(s)
                            pending = ""
                    elif len(pending.strip()) > config.SENTENCE_MIN_LEN:
                        idx = max(pending.rfind("，"), pending.rfind(","))
                        if idx + 1 >= MIN_CHUNK_CHARS:      # 逗号前的部分也要够长
                            head, pending = pending[:idx + 1], pending[idx + 1:]
                            emit("reply", {"text": head})
                            submit_tts(head)
            except Exception as exc:
                print(f"AI 流式失败: {exc}")

            full_reply = "".join(full_reply_parts).strip()
            if not full_reply:
                full_reply = AI_EMPTY_NOTICE
                emit("reply", {"text": full_reply})
            elif pending.strip():
                emit("reply", {"text": pending.strip()})
                submit_tts(pending.strip())

            with tts_lock:
                ready[tts_seq] = None        # 结束哨兵排在最后一句之后
                tts_lock.notify_all()
            emitter.join(timeout=config.TTS_QUEUE_TIMEOUT)
            tts_pool.shutdown(wait=False)
            _screen_idle()          # 音频发送完毕 → 设备屏回到待机

            emotion = extract_emotion_from_text(full_reply)
            _set_robot_emotion(emotion)
            _log_emotion_event(store, user_id, emotion, full_reply)
            _log_dialogue_event(store, user_id, message, full_reply, phase)
            memory.add_turn(user_text=message, assistant_reply=full_reply, phase=phase)
            if finished:
                # 收尾这一餐：**finish() 是幂等的**，只有第一次返回会话 ——
                # 所以无论用户话术、超时、还是接口重入触发，都只会统计一次。
                _close_meal(store, user_id, reason=finish_reason or "user_said_finished",
                            turns=memory.turns, phase=phase)
                _clear_finished_question()
            _screen_turn(message, full_reply)
            emit("done", {"phase": phase, "finished": finished,
                          "memory_turns": len(memory.turns), "emotion": emotion,
                          "full_reply": full_reply})
            _compress_memory_soon(memory, client)   # 后台压缩，不占用这一轮的响应时间
            events.put(None)     # 结束哨兵

        threading.Thread(target=producer, name="dialogue-producer", daemon=True).start()

        # 异步侧只做转发：to_thread 让阻塞 get 不占用事件循环，每个事件立即刷出
        while True:
            item = await asyncio.to_thread(events.get)
            if item is None:
                break
            kind, payload = item
            yield _sse(kind, payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("")
async def dialogue(request: Request):
    """非流式对话（供 CLI / 简单客户端）。"""
    body = await request.json()
    user_id = str(body.get("user_id") or "default")
    message = str(body.get("message") or "")
    reset = bool(body.get("reset"))

    memory = ConversationMemory.load(user_id)
    store = HealthStore()

    if reset:
        _pop_session(user_id)
        memory.turns = []
        memory.summary = ""
        memory.save()
        _set_robot_emotion("neutral")
        return {"ok": True, "reset": True,
                "reply": "已重置，开始新一轮正念进食引导。", "finished": False, "emotion": "neutral"}

    sess = _get_session(user_id)
    from ..core.meal import meal_manager
    meal = meal_manager.current(user_id)
    phase, meal_active = (meal.phase.value, True) if meal else ("idle", False)

    if not message.strip():
        prompt = _ai_opening(phase, meal_active)   # 开场白由 AI 生成，不朗读本地脚本
        return {"ok": True, "reply": prompt, "phase": phase,
                "finished": False, "memory_turns": len(memory.turns),
                "emotion": extract_emotion_from_text(prompt)}

    # 一餐的生命周期与流式接口共用同一套规则（见 _advance_meal）
    phase, finished, finish_reason = _advance_meal(user_id, message)
    meal_active = phase != "idle"
    sess.note(message, phase, finished)

    reply, emotion = "", "neutral"
    client = get_ai_client()
    if isinstance(client, LocalEchoClient):
        return {"ok": True, "reply": AI_UNCONFIGURED_NOTICE,
                "phase": phase, "finished": finished, "memory_turns": len(memory.turns),
                "emotion": "neutral", "unconfigured": True}
    ai_prompt = (
        _build_llm_prompt(message, phase, meal_active,
                          (memory.turns[-1].get("assistant_reply") if memory.turns else "") or "")
        + "\n\n【重要】请在回复的最后一行用 [EMOTION:xxx] 标记回复的主要情绪。"
          f"可选: {', '.join(sorted(VALID_EMOTION))}。示例：[EMOTION:happy]"
    )
    # 真实 AI 客户端的 chat() 没有 user_text 参数（那是本地规则客户端专用的），
    # 多传会直接抛 TypeError → 接口 500。
    raw = client.chat(ai_prompt, context=memory.context_text() or None)
    if raw and not raw.startswith("抱歉"):
        m = re.search(r"\[EMOTION:(\w+)\]", raw)
        if m and m.group(1) in VALID_EMOTION:
            emotion = m.group(1)
            reply = re.sub(r"\s*\[EMOTION:\w+\]\s*", "", raw).strip()
        else:
            reply = raw
            emotion = extract_emotion_from_text(reply)
    else:
        reply = raw or AI_EMPTY_NOTICE
    if emotion == "neutral":
        emotion = extract_emotion_from_text(reply)
    _set_robot_emotion(emotion)
    _log_emotion_event(store, user_id, emotion, reply)
    _log_dialogue_event(store, user_id, message, reply, phase)
    memory.add_turn(user_text=message, assistant_reply=reply, phase=phase)
    if finished:
        # 收尾这一餐（幂等：只统计一次）
        _close_meal(store, user_id, reason=finish_reason or "user_said_finished",
                    turns=memory.turns, phase=phase)
        _clear_finished_question()
    _screen_turn(message, reply)
    _compress_memory_soon(memory, client)      # 后台压缩，不占用这一轮的响应时间
    return {"ok": True, "reply": reply, "phase": phase,
            "finished": finished, "memory_turns": len(memory.turns), "emotion": emotion}


@router.post("/start")
async def start_session(request: Request):
    """显式开始一餐：进入餐前阶段，后面按真实进度推进。"""
    body = await request.json()
    user_id = str(body.get("user_id") or "default")
    _pop_session(user_id)
    _get_session(user_id)
    from ..core.meal import meal_manager
    meal = meal_manager.start(user_id, force=True)
    return {"ok": True, "phase": meal.phase.value,
            "prompt": _ai_opening(meal.phase.value, True)}


@router.get("/meal")
async def meal_state(user_id: str = "default"):
    """当前这一餐的状态（前端的阶段提示/调试用）。"""
    from ..core.meal import meal_manager
    return meal_manager.state(user_id)


@router.post("/meal/finish")
async def meal_finish(request: Request):
    """显式结束这一餐并统计一次（供前端按钮/超时收尾调用；幂等）。"""
    body = await request.json()
    user_id = str(body.get("user_id") or "default")
    reason = str(body.get("reason") or "manual")
    recorded = _close_meal(HealthStore(), user_id, reason=reason, turns=None, phase="")
    _pop_session(user_id)
    return {"ok": True, "recorded": recorded}
