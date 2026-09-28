"""自主互动 —— AI 不等用户开口，按情绪/进食数据主动说话并做表情。

级别与触发条件见 app/core/interaction_config.py（AUTONOMY_LEVELS / TRIGGER_PROMPTS）。
信号取自 DisplayState.metrics()：与设备屏第一页、第二页同源，
因此"页面上看到的数值"就是 AI 主动开口的依据。

主动事件通过 SSE（/api/agent/proactive）推给设备页，由前端播放语音并切换表情。
"""
from __future__ import annotations

import base64
import queue
import threading
import time
from typing import Optional

from . import interaction_config as ic


class AutonomyService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._subscribers: list[queue.Queue] = []
        self._last_check = 0.0
        self._last_spoke = 0.0
        self._last_reason = ""
        self._idle_asked_at = float("-inf")   # 上次询问"是否吃完"的时间（没问过）
        self._last_reason_at: dict[str, float] = {}   # 每个触发原因的上一次开口时间（冷却用）
        self._emotion_seen = ""          # 上一次读到的情绪（去抖）
        self._emotion_streak = 0         # 同一种情绪连续读到几次
        self._emotion_fired = ""         # 上一次为哪种情绪开过口（同一种不复读）
        self._last_said = ""             # 上一句主动互动，防复读
        # 这一餐的状态机建议"该问一句是不是吃完了"（见 _feed_meal_state）
        self._meal_suggests_finished = False

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_spoke = time.time()      # 刚启动不立刻主动搭话
        self._thread = threading.Thread(target=self._loop, name="autonomy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        settings = ic.get_settings()
        return {
            "running": self._running,
            "autonomy": settings.autonomy,
            "autonomy_name": settings.autonomy_info["name"],
            "last_reason": self._last_reason,
            "last_spoke_at": round(self._last_spoke, 1),
        }

    # ---------- "是不是吃完了"这个问题的状态 ----------
    _FINISHED_ASK_WINDOW = 180.0

    def asked_finished_recently(self) -> bool:
        """最近 3 分钟内是否问过"是不是吃完了"。

        用于把用户只回一句"对/嗯/是的"也算作吃完（否则必须说"我吃完了"才行）。
        """
        return (self._last_reason == "finished_check"
                and time.time() - self._idle_asked_at <= self._FINISHED_ASK_WINDOW)

    def clear_finished_question(self) -> None:
        """已经据此记过一次进食，别让同一句"对"重复计数。"""
        self._last_reason = ""

    # ---------- SSE 订阅 ----------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)

    # ---------- 主循环 ----------
    def _loop(self) -> None:
        while self._running:
            wait = 5.0
            try:
                wait = self._tick()
            except Exception as exc:
                print(f"[autonomy] 异常: {exc}", flush=True)
            time.sleep(max(1.0, wait))

    def _tick(self) -> float:
        level = ic.get_settings().autonomy_info
        # 这一餐的生命周期（摄像头看到在吃就开餐、挂太久就收尾统计）**与"要不要主动说话"无关**，
        # 所以放在 enabled 判断之前 —— 把自主互动关掉的用户同样该有用餐记录。
        # 顺带：这个 2 秒级的轮询节奏比原来 15 秒更贴合"连续看到在吃 25 秒"的判定。
        metrics = self._metrics_snapshot()
        self._feed_meal_state(metrics)

        if not level["enabled"]:
            return 15.0
        now = time.time()
        if now - self._last_check < level["interval"]:
            return level["interval"] - (now - self._last_check)
        self._last_check = now

        if now - self._last_spoke < level["min_gap"]:
            return 2.0
        if not self._can_speak():
            return 2.0
        reason = self._decide(level, metrics, now)
        if reason:
            self._speak(reason)
        return 2.0

    @staticmethod
    def _meal():
        """当前这一餐的会话（没有进行中的一餐时返回 None）。"""
        try:
            from ..core.meal import meal_manager
            return meal_manager.current("default")
        except Exception:
            return None

    def _meal_phase(self) -> str:
        meal = self._meal()
        return meal.phase.value if meal is not None else ""

    @staticmethod
    def _feed_meal_state(metrics: dict) -> None:
        """按摄像头指标推进/开餐/收尾这一餐（超时也要能落地，否则"用餐次数"永远是 0）。

        注意**没有进行中的一餐时也要调用**：连续看到进食就自动开一餐（见
        `meal_manager.note_metrics`），否则"没人喊开饭就不存在这一餐"。
        """
        try:
            from ..core.meal import meal_manager
            uid = "default"
            suggestion = meal_manager.note_metrics(uid, metrics or {})
            if suggestion == "ask_finished":
                # 餐中长时间没在吃 → 让 AI 去问一句"是不是吃完了"
                # （用户回"嗯/对"时由 _confirmed_by_answer 收尾并统计）
                autonomy_service._meal_suggests_finished = True
            reason = meal_manager.expire_reason(uid) if meal_manager.is_active(uid) else ""
            if reason:
                # 人走了/一直没回应（10 分钟无动静）或整餐挂太久：直接收尾并统计，
                # 避免漏记这一餐（用餐次数永远停在 0）
                from ..api.dialogue import _close_meal
                from .db import HealthStore
                _close_meal(HealthStore(), uid, reason=reason)
        except Exception as exc:
            print(f"[meal] 状态推进失败: {exc}", flush=True)

    @staticmethod
    def _can_speak() -> bool:
        """用户正在说话 / 机器人正在思考或播报时一律让路。

        以前判断的是 `face_state() == "idle"`，有两个毛病：
          1. **漏判**：用户正在录音时浏览器还没上传，服务端毫不知情，
             表情仍是 idle → AI 在用户话说到一半时插嘴。
             现在前端会主动上报「正在听」（POST /api/device/listen），
             并且 display_state.is_busy() 把 listening 也算进去。
          2. **误判**：set_emotion() 会用情绪表情覆盖待机态
             （face_state() 返回 "happy"/"焦虑" 等），于是每次情绪变化后
             有 6 秒窗口 AI 完全不能主动开口 —— 白丢很多互动机会。
             is_busy() 看的是真实忙碌原因，不受情绪覆盖影响。
        """
        try:
            from ..display.state import display_state
            return not display_state.is_busy()
        except Exception:
            return True

    def _decide(self, level: dict, metrics: dict, now: float) -> str:
        """按当前级别与实时指标决定是否主动开口，返回触发原因（空串=不开口）。

        **顺序很重要**：先处理"这一餐本身该做什么"（吃完确认 → 阶段引导），
        再处理情绪/速度这类即时信号。否则会出现"用户早就吃完了，
        AI 还在提醒慢点嚼"这种张冠李戴。

        另外两条硬规则：
          * **不在用餐（或已经餐后）就不谈进食** —— 不吃着的时候提醒"慢点嚼"、
            "为你开心"都毫无道理（日志里连着 5 条 celebrate 就是这么来的）；
          * 每种原因都有冷却，情绪类还要"情绪真的变了"才开口，避免做复读机。
        """
        # 0) 这一餐的状态机明确建议问一句"是不是吃完了"
        if self._meal_suggests_finished:
            self._meal_suggests_finished = False
            self._idle_asked_at = now          # asked_finished_recently() 依赖它
            return "finished_check"

        meal = self._meal()
        meal_phase = meal.phase.value if meal is not None else ""

        # 1) 餐前：还没开吃 → 不要谈咀嚼/速度，而是做餐前引导
        if meal_phase == "pre_meal":
            gap = float(level.get("checkin_gap") or 0.0)
            if gap and now - self._last_spoke >= gap and self._ready_to_say("pre_meal_guide", now):
                return "pre_meal_guide"
            return ""

        # 2) 情绪回应：任何阶段都可以，但要"稳定两拍 + 刚变化"（防摄像头噪声复读）
        reason = self._emotion_reason(level, metrics, now)
        if reason:
            return reason

        # 3) 进食相关（是否吃完 / 咀嚼速度 / 关心一句）：**只在餐中谈**
        if meal_phase != "mid_meal":
            return ""
        # "停下来了吗"由 core/meal.py 的 note_metrics 统一判（唯一一处计时 + 冷却），
        # 通过 _meal_suggests_finished 传过来（见 _tick），这里不再自己数秒。
        if level["abnormal_eating"]:
            if (str(metrics.get("chew_level") or "") == "fast"
                    and self._ready_to_say("chew_fast", now)):
                return "chew_fast"
            if (str(metrics.get("eat_level") or "") == "fast"
                    and self._ready_to_say("eat_fast", now)):
                return "eat_fast"
        gap = float(level.get("checkin_gap") or 0.0)
        if gap and now - self._last_spoke >= gap and self._ready_to_say("checkin", now):
            return "checkin"
        return ""

    def _ready_to_say(self, reason: str, now: float) -> bool:
        """同一个原因过了冷却期没有（防复读机）。"""
        cooldown = float(ic.TRIGGER_COOLDOWN.get(reason, 0.0))
        return now - self._last_reason_at.get(reason, float("-inf")) >= cooldown

    def _emotion_reason(self, level: dict, metrics: dict, now: float) -> str:
        """情绪触发去抖：连续 N 拍读到同一种情绪才算数，同一种情绪只回应一次。

        摄像头单帧误判很常见（DB 里的 emotion_detect 大量是 confidence 0.3~0.5 的
        surprise），旧版每 15 秒就能拿它夸用户一次。
        """
        emotion = str(metrics.get("emotion_key") or metrics.get("emotion") or "")
        if emotion == self._emotion_seen:
            self._emotion_streak += 1
        else:
            self._emotion_seen, self._emotion_streak = emotion, 1
        if not emotion or self._emotion_streak < ic.EMOTION_STABLE_SAMPLES:
            return ""
        if emotion == self._emotion_fired:
            return ""                       # 这一波情绪已经回应过，不重复
        if emotion in level["comfort_emotions"]:
            reason = "comfort"
        elif emotion in level["celebrate_emotions"]:
            reason = "celebrate"
        else:
            # 回到中性/未知：允许同一种情绪下次再被回应
            self._emotion_fired = ""
            return ""
        if not self._ready_to_say(reason, now):
            self._emotion_fired = emotion
            return ""
        self._emotion_fired = emotion
        return reason

    def _speak(self, reason: str) -> None:
        settings = ic.get_settings()
        metrics = self._metrics_snapshot()
        recent = self._recent_dialogue()
        prompt = self._build_prompt(settings.mode_info, ic.TRIGGER_PROMPTS.get(reason, ""),
                                    metrics, recent=recent, last_said=self._last_said)
        try:
            from .ai_client import LocalEchoClient, get_ai_client
            client = get_ai_client()
            if isinstance(client, LocalEchoClient):
                # 未配置真实 AI：宁可不主动开口，也不用本地规则话术冒充
                return
            reply = (client.chat(prompt, context=recent or None) or "").strip()
        except Exception as exc:
            print(f"[autonomy] 生成失败: {exc}", flush=True)
            return
        if not reply:
            return

        emotion = self._emotion(reply)
        audio_b64 = self._tts(client, reply)
        # 合成要 1~2 秒：这段时间用户完全可能已经开口了，发布前再确认一次，
        # 否则就是"用户话说到一半，AI 插进来"。
        if not self._can_speak():
            print(f"[autonomy] 放弃主动开口（{reason}）：用户已经开始说话了", flush=True)
            return
        self._last_spoke = time.time()
        self._last_reason = reason
        self._last_reason_at[reason] = self._last_spoke
        self._last_said = reply

        try:
            from ..display.service import screen_service
            screen_service.on_emotion(emotion)
            screen_service.on_turn("（饭崽主动）", reply)
            screen_service.on_speaking(max(2.0, len(reply) * 0.18))
        except Exception:
            pass

        self._publish({"text": reply, "emotion": emotion, "reason": reason,
                       "format": self._tts_format(reply),
                       "audio_b64": audio_b64, "ts": time.time()})
        print(f"[autonomy] 主动互动（{reason}）：{reply}", flush=True)

    @staticmethod
    def _metrics_snapshot() -> dict:
        try:
            from ..display.state import display_state
            return display_state.metrics()
        except Exception:
            return {}

    @staticmethod
    def _recent_dialogue() -> str:
        """最近几轮聊过的内容（让主动开口能接上刚才的话题，而不是自说自话）。"""
        try:
            from .memory import ConversationMemory
            return (ConversationMemory.load("default").context_text() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _build_prompt(role: dict, trigger: str, metrics: dict,
                      recent: str = "", last_said: str = "") -> str:
        """主动开口的提示：**必须带上当前真实状态**，否则容易说出与场景不符的话
        （比如用户还没开吃就提醒"慢点嚼"，或者已经吃完了还在劝"再吃点"）。

        状态来源五处：当前这一餐的阶段（**含"不在用餐"这个事实**）、摄像头指标、
        时间、最近聊过的内容、以及自己上一句主动说的话（避免复读）。
        """
        parts = [f"你是「正念饭崽」，现在扮演：{role.get('role', '')}。", role.get("role_prompt", "")]
        meal = None

        # ---- 这一餐处在哪个阶段（最影响该说什么）----
        try:
            from ..core.meal import meal_manager
            meal = meal_manager.current("default")
            if meal is not None:
                parts.append(f"当前这一餐的进度：{meal.phase_zh}（已进行 {meal.duration_min} 分钟，"
                             f"已经聊过 {meal.turns} 轮）。")
                if meal.guide():
                    parts.append(meal.guide())
        except Exception:
            meal = None
        if meal is None:
            # 没在用餐就明确说清楚：否则模型会顺着"进食教练"的角色
            # 对着没在吃饭的人讲咀嚼速度、饱腹感。
            parts.append("当前**不在用餐**（没有进行中的一餐）："
                         "不要提咀嚼速度、进食速度、几分饱，也不要问「吃完了吗」；"
                         "这一句只做日常关心或回应他的情绪。")

        # ---- 摄像头观察到的实时状态 ----
        state = []
        emo = metrics.get("emotion") or metrics.get("emotion_key")
        if emo:
            state.append(f"用户情绪：{emo}")
        fc = metrics.get("face_count")
        if fc is not None:
            state.append("画面里有人" if fc else "画面里看不到人")
        if metrics.get("chews_per_min") is not None:
            lvl = {"fast": "偏快", "slow": "偏慢", "normal": "正常"}.get(
                str(metrics.get("chew_level") or ""), "")
            state.append(f"咀嚼：{metrics['chews_per_min']} 次/分" + (f"（{lvl}）" if lvl else ""))
        if metrics.get("bites_per_min") is not None:
            lvl = {"fast": "偏快", "slow": "偏慢", "normal": "正常"}.get(
                str(metrics.get("eat_level") or ""), "")
            state.append(f"送食：{metrics['bites_per_min']} 次/分" + (f"（{lvl}）" if lvl else ""))
        if metrics.get("chew_count") is not None:
            state.append(f"累计咀嚼 {metrics['chew_count']} 次")
        if state:
            parts.append("当前观察到的状态：" + "；".join(state) + "。")

        # ---- 最近聊过什么（主动开口也要接得上刚才的话题）----
        if recent:
            parts.append("你们最近聊过的内容（供参考，别复述）：" + recent[:200])

        # ---- 时间（决定是早餐/午餐/晚餐/宵夜）----
        try:
            import time as _t
            h = _t.localtime().tm_hour
            slot = ("早上" if 5 <= h < 10 else "中午" if 10 <= h < 14 else
                    "下午" if 14 <= h < 17 else "傍晚" if 17 <= h < 19 else
                    "晚上" if 19 <= h < 22 else "深夜")
            parts.append(f"现在是{slot}。")
        except Exception:
            pass

        if last_said:
            parts.append(f"你上一次主动说的是：「{last_said[:40]}」，这次换一个角度，别重复。")
        parts.append(f"现在请你主动开口（用户没有说话）。{trigger}")
        parts.append("**要求**："
                     "①话要贴合上面这个阶段和状态，别说不合时宜的话；"
                     "②只说 1~2 句、总字数不超过 40 个汉字；"
                     "③语气温暖自然像老朋友，不要说“监测到”“数据显示”“根据数据”这类词；"
                     "④不要重复之前说过的提醒，换一个角度。")
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _emotion(text: str) -> str:
        try:
            from ..api.dialogue import extract_emotion_from_text
            return extract_emotion_from_text(text)
        except Exception:
            return "neutral"

    @staticmethod
    def _tts(client, text: str) -> Optional[str]:
        """合成语音并返回 base64；命中缓存直接返回（走统一 TTS 链：Qwen3-TTS-Flash）。"""
        if not ic.tts_enabled():
            return None            # 语音播报关了：只推文字与表情，不打 TTS
        try:
            from .. import config
            from ..voice import tts_cache
            from ..voice.tts import cache_voice, synthesize
            voice = cache_voice(client)
            cached = tts_cache.get(text, voice)
            if cached is None:
                out = config.PATHS.media_dir / "audio" / f"proactive-{int(time.time() * 1000)}.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                if not synthesize(text, out):
                    return None
                cached = out.read_bytes()
                tts_cache.put(text, voice, cached)
                out.unlink(missing_ok=True)
            return base64.b64encode(cached).decode("ascii")
        except Exception:
            return None

    @staticmethod
    def _tts_format(text: str) -> str:
        """给前端用的容器格式（按实际字节判断：Qwen3-TTS 出 WAV）。"""
        try:
            from ..voice import tts_cache
            from ..voice.tts import cache_voice, detect_format
            cached = tts_cache.get(text, cache_voice(None))
            return detect_format(cached)
        except Exception:
            return "mp3"


autonomy_service = AutonomyService()
