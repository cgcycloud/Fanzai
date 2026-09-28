"""交互配置 —— 运行模式 / 自主互动 / AI 角色 / 亮度音量（可调参数集中于此）。

对应设备屏"第一页下滑设置面板"：
  MODES            运行模式：进食检测（开摄像头）/ 日常聊天（关摄像头），各自绑定 AI 角色
  AUTONOMY_LEVELS  自主互动：激进 / 正常 / 关闭 —— AI 不等用户开口就主动说话的频率与触发条件
  TRIGGER_PROMPTS  主动互动的原因 → 给 AI 的提示词
  BRIGHTNESS/VOLUME 亮度与音量的取值范围

运行期选择持久化到 <data_dir>/agent_settings.json，进程内带缓存，
渲染循环（30fps）可直接 get_settings() 读取，不会反复读磁盘。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import PATHS

# ===================== 运行模式 =====================
MODE_MEAL = "meal"
MODE_CHAT = "chat"

MODES: dict[str, dict] = {
    MODE_MEAL: {
        "name": "进食检测",
        "desc": "开摄像头，看咀嚼/进食/情绪",
        "vision": True,
        "autonomy_default": "normal",
        "role": "正念进食教练",
        "role_prompt": (
            "当前是【进食检测模式】：摄像头已开启，你会收到用户的咀嚼、进食速度与情绪数据。"
            "你以正念进食教练的身份说话，第一先问一问今天吃的是什么，结合用户目前的情绪，围绕慢下来、觉察饥饿与饱足、分辨情绪性进食给出简短建议；问过的问题就不用再反复重复问，"
            "表达方式可以灵活多样，避免语言重复性，不要机械地套用模板。"
            "引用进食数据时只依据给你的数值，不要编造，也不要诊断疾病或调整用药。"
        ),
    },
    MODE_CHAT: {
        "name": "日常聊天",
        "desc": "关摄像头，只做陪伴聊天",
        "vision": False,
        "autonomy_default": "off",
        "role": "日常陪伴伙伴",
        "role_prompt": (
            "当前是【日常聊天模式】：摄像头已关闭，你没有进食监测数据。"
            "你以温暖的日常陪伴伙伴身份说话，聊情绪与日常；"
            "不要提到摄像头、监测，也不要假装看到了进食数据。"
        ),
    },
}


# ===================== 自主互动级别 =====================
AUTONOMY_AGGRESSIVE = "aggressive"
AUTONOMY_NORMAL = "normal"
AUTONOMY_OFF = "off"

AUTONOMY_LEVELS: dict[str, dict] = {
    AUTONOMY_AGGRESSIVE: {
        "name": "激进",
        "desc": "频繁地主动互动",
        "enabled": True,
        "interval": 6.0,        # 每 6 秒评估一次
        "min_gap": 15.0,        # 两次主动说话至少间隔 15 秒
        "checkin_gap": 60.0,    # 一直没有信号时，每 60 秒也关心一句
        "comfort_emotions": ("sad", "angry", "cry", "anxiety", "aggrieved",
                             "hatred", "irritated"),
        "celebrate_emotions": ("happy", "surprise", "love"),
        "abnormal_eating": True,
        "eating_idle_check": True,
    },
    AUTONOMY_NORMAL: {
        "name": "正常",
        "desc": "正常的互动",
        "enabled": True,
        "interval": 15.0,
        "min_gap": 45.0,
        "checkin_gap": 180.0,
        "comfort_emotions": ("sad", "angry", "cry", "anxiety", "aggrieved",
                             "hatred", "irritated"),
        "celebrate_emotions": ("happy",),
        "abnormal_eating": True,
        "eating_idle_check": True,
    },
    AUTONOMY_OFF: {
        "name": "关闭",
        "desc": "用户说话才给出反应",
        "enabled": False,
        "interval": 30.0,
        "min_gap": 0.0,
        "checkin_gap": 0.0,
        "comfort_emotions": (),
        "celebrate_emotions": (),
        "abnormal_eating": False,
        "eating_idle_check": False,     # 关闭自主互动时，不主动询问是否吃完
    },
}

# 触发原因 → 给 AI 的提示（"主动说什么"由这里决定）
# 注意：这些只是**理由**，具体台词交给模型；且 autonomy 会连同当前这一餐的阶段
# 与摄像头状态一起注入，所以这里不用再重复"现在是什么情况"。
TRIGGER_PROMPTS: dict[str, str] = {
    "pre_meal_guide": "用户已经说要吃饭了、但还没真正开动。"
                      "请做一句**餐前**引导（三选一，不要都做）：陪他做一次深呼吸、"
                      "帮他分辨是胃在饿还是心在饿、或者问一句今天准备吃什么。",
    "comfort": "用户现在的情绪偏低落或紧张，请先接纳情绪，避免语言模版化的前提下，再邀请他做三次深呼吸，好好吃饭。",
    "celebrate": "用户现在情绪不错，请为这份好心情共情，避免语言模版化的前提下，并提醒用正念的方式进食。",
    "chew_fast": "用户咀嚼速度偏快，避免语言模版化的前提下，请温和提醒放慢、多咀嚼几下再咽。",
    "eat_fast": "用户进食速度偏快，避免语言模版化的前提下，请温和提醒放慢，注意感受饱足信号。",
    "finished_check": "用户已经连续好几分钟几乎没有咀嚼（每分钟不足 5 次）。"
                      "请用一句话确认他是不是已经吃完了；"
                      "**只问这一件事，不要顺带说别的**，他回一句「嗯/对」就可以了。",
    "checkin": "没有明显信号，主动关心一句或者询问用户此刻的状态。避免语言模版化的前提下，提醒用户别一直看电视或者看手机，注意进食。",
}

# 同一种主动互动的最短间隔（秒）—— 防"复读机"。
# 真实踩过：摄像头把噪声识别成惊讶/开心，emotion 一直显示同一种情绪，
# 于是 autonomy 每隔 15 秒就"为你开心"一次（日志里连着 5 条 celebrate）。
# 现在除了情绪本身要**先变化**才触发，每个原因还有一道冷却闸。
TRIGGER_COOLDOWN: dict[str, float] = {
    "comfort": 600.0,
    "celebrate": 900.0,
    "chew_fast": 300.0,
    "eat_fast": 300.0,
    "checkin": 0.0,         # 由各档位的 checkin_gap 控制
    "pre_meal_guide": 0.0,  # 同上
    "finished_check": 0.0,  # 由 EATING_IDLE.cooldown_sec 控制
}

# 情绪触发前要求"连续几次读到同一种情绪"（摄像头单帧噪声很常见，去抖用）
EMOTION_STABLE_SAMPLES = 2


# ===================== 硬件调节范围 =====================
BRIGHTNESS = {"min": 20, "max": 100, "default": 100, "step": 5}
VOLUME = {"min": 0, "max": 100, "default": 70, "step": 5}
# AI 语速：edge-tts 的 rate 百分比，负数更慢
SPEECH_RATE = {"min": -50, "max": 50, "default": 0, "step": 5}


# ===================== 语音唤醒 =====================
WAKE_SINGLE = "single"
WAKE_CONTINUOUS = "continuous"

WAKE_MODES: dict[str, dict] = {
    WAKE_SINGLE: {
        "name": "单句",
        "desc": "唤醒后听一句就回到待唤醒",
    },
    WAKE_CONTINUOUS: {
        "name": "连续",
        "desc": "唤醒后持续对话，说「再见」或静音结束",
    },
}

# 默认唤醒词：产品名是「正念饭崽」，所以「你好饭崽」为主，兼容旧的「你好正念」。
# 刻意不含两字词（如「饭崽」）：两字词谐音太多，嘈杂环境下极易误唤醒；
# 确实想用可以自己加进 agent_settings.json，判定会走更严格的短词规则。
DEFAULT_WAKE_WORDS: list[str] = ["你好饭崽", "你好正念"]

WAKE = {
    "enabled_default": True,
    "mode_default": WAKE_SINGLE,
    "words": DEFAULT_WAKE_WORDS,
    "cooldown_sec": 2.0,          # 两次唤醒之间的最短间隔
    "max_hits_per_min": 5,        # 每分钟最多唤醒几次（噪声环境下兜住"被刷醒"）
    "chunk_ms": 450,              # 浏览器切片上传周期（越小越灵敏，越大越省）
    "gate_silence": True,         # 前端只上传"有声音"的片段（静音不上传，省算力又抗噪）
    # 连续模式：这么久没听到人声才回待唤醒。别调太小 —— 用户吃饭时说话有停顿，
    # 15 秒实测太紧（"只能单句对话"的体感来源之一），30 秒更接近"一直在听"。
    "continuous_idle_sec": 30.0,
    # 连续模式：**播报结束后再等这么久**才重新接受下一句（用户要求 1 秒）。
    # 这 1 秒是留给扬声器尾音/混响的：太短会把机器人自己的尾音当成用户开口。
    "continuous_resume_sec": 1.0,
    "stop_words": ["再见", "拜拜", "不聊了", "结束对话", "不用了","别说话", "停", "停止", "安静", "闭嘴"],  # 连续模式下用户说这些就结束本轮对话
    # 命中唤醒词后的应答：先出声回应，再开麦克风听用户说话（顺序很关键，
    # 先开麦会把应答本身录进去）。启动时会被预合成，正常是秒回。
    #
    # **句号不能省**：实测 qwen3-tts-flash 对「我在」这种两字输入会"赶着收尾"，
    # 5 次里就有 1 次在能量 85% 处直接掐断（听感就是"我在……"后面卡了半个字），
    # 时长也在 0.40~0.80s 之间乱跳；写成「我在。」后 5/5 都是自然收尾、时长稳定。
    # 多一个句号不影响听感（用户听到的仍是"我在"），所以这里保留标点。
    "ack_text": "我在。",
}

MAX_WAKE_WORDS = 8


# ===================== AI 音色 =====================
# 发声引擎只剩一个：云端 Qwen3-TTS-Flash（阿里云百炼，见 app/voice/qwen_tts_engine.py）。
# 原来的多音色切换（Kokoro / Matcha / VITS / Piper / edge-tts）与「试听」已全部移除，
# 所以这里不再有 TTS_ENGINE_KEYS / KOKORO_ZH_VOICES / VITSLL_SPEAKERS 这些表。
# 音色本身改由 ai_config.json 的 `tts_voice` 配置（手改该文件即可），不在设备屏切换。
TTS_PREVIEW_TEXT = ""            # 试听功能已移除，保留常量仅为兼容旧引用


def _clean_words(raw) -> list[str]:
    """唤醒词清洗：去空白、去重、限长限量（避免面板误传把语法撑得过大）。"""
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace("、", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_WAKE_WORDS)
    out: list[str] = []
    for item in raw:
        word = str(item or "").strip()
        if not word or len(word) > 8 or word in out:
            continue
        out.append(word)
        if len(out) >= MAX_WAKE_WORDS:
            break
    return out or list(DEFAULT_WAKE_WORDS)

# ===================== 进食停顿检查 =====================
# 连续一段时间咀嚼频率低于阈值 → 主动问用户"是不是吃完了"。
# 注意 chews_per_min 是"最近 60 秒内嚼了几下"的滑动窗口值：
# 停止咀嚼后它必然在 1 分钟内降到 0，这是定义如此，不是计数器被清零。
# **这套阈值只在 core/meal.py::note_metrics 里用**（暂停判定唯一一处）；
# autonomy 只负责把 meal_manager 的建议说出口，不再自己计时，否则两边各自计时、
# 各自冷却，用户停一会儿就会被反复追问。
EATING_IDLE = {
    "enabled": True,
    "chews_per_min": 5.0,     # 低于这个频率视为"没在吃"
    "sustain_sec": 120.0,     # 持续这么久才问（用户要求：两分钟）
    "cooldown_sec": 300.0,    # 问过一次后，至少隔这么久才会再问
}


@dataclass(frozen=True)
class AgentSettings:
    mode: str = MODE_MEAL
    autonomy: str = AUTONOMY_NORMAL
    tts_enabled: bool = True         # 语音播报开关（下滑设置面板可关：只出文字不出声）
    brightness: int = BRIGHTNESS["default"]
    volume: int = VOLUME["default"]
    speech_rate: int = SPEECH_RATE["default"]
    wake_enabled: bool = WAKE["enabled_default"]
    wake_mode: str = WAKE["mode_default"]
    wake_words: tuple[str, ...] = tuple(DEFAULT_WAKE_WORDS)

    @property
    def mode_info(self) -> dict:
        return MODES.get(self.mode, MODES[MODE_MEAL])

    @property
    def autonomy_info(self) -> dict:
        return AUTONOMY_LEVELS.get(self.autonomy, AUTONOMY_LEVELS[AUTONOMY_OFF])

    @property
    def wake_mode_info(self) -> dict:
        return WAKE_MODES.get(self.wake_mode, WAKE_MODES[WAKE_SINGLE])

    @property
    def vision_enabled(self) -> bool:
        return bool(self.mode_info.get("vision"))

    def public_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_name": self.mode_info["name"],
            "role": self.mode_info.get("role", ""),
            "vision": self.vision_enabled,
            "autonomy": self.autonomy,
            "autonomy_name": self.autonomy_info["name"],
            "tts_enabled": self.tts_enabled,
            "brightness": self.brightness,
            "volume": self.volume,
            "speech_rate": self.speech_rate,
            "wake_enabled": self.wake_enabled,
            "wake_mode": self.wake_mode,
            "wake_mode_name": self.wake_mode_info["name"],
            "wake_words": list(self.wake_words),
        }


def _clamp(value, spec: dict, fallback: int) -> int:
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(spec["min"], min(spec["max"], v))


class AgentSettingsStore:
    """读写 <data_dir>/agent_settings.json，带进程内缓存。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (PATHS.data_dir / "agent_settings.json")
        self._lock = threading.Lock()
        self._cache: AgentSettings | None = None

    def load(self) -> AgentSettings:
        with self._lock:
            if self._cache is None:
                self._cache = self._read()
            return self._cache

    def save(self, **changes) -> AgentSettings:
        """局部更新：只传要改的字段。切换模式时会重置为该模式的默认互动级别。"""
        with self._lock:
            cur = self._cache if self._cache is not None else self._read()
            data = {"mode": cur.mode, "autonomy": cur.autonomy,
                    "tts_enabled": cur.tts_enabled,
                    "brightness": cur.brightness, "volume": cur.volume,
                    "speech_rate": cur.speech_rate,
                    "wake_enabled": cur.wake_enabled, "wake_mode": cur.wake_mode,
                    "wake_words": cur.wake_words}

            mode = changes.get("mode")
            if mode in MODES:
                data["mode"] = mode
                data["autonomy"] = MODES[mode]["autonomy_default"]
            if changes.get("autonomy") in AUTONOMY_LEVELS:
                data["autonomy"] = changes["autonomy"]
            if "tts_enabled" in changes:
                data["tts_enabled"] = bool(changes["tts_enabled"])
            if "brightness" in changes:
                data["brightness"] = _clamp(changes["brightness"], BRIGHTNESS, cur.brightness)
            if "volume" in changes:
                data["volume"] = _clamp(changes["volume"], VOLUME, cur.volume)
            if "speech_rate" in changes:
                data["speech_rate"] = _clamp(changes["speech_rate"], SPEECH_RATE, cur.speech_rate)
            if "wake_enabled" in changes:
                data["wake_enabled"] = bool(changes["wake_enabled"])
            if changes.get("wake_mode") in WAKE_MODES:
                data["wake_mode"] = changes["wake_mode"]
            if "wake_words" in changes:
                data["wake_words"] = tuple(_clean_words(changes["wake_words"]))

            settings = AgentSettings(**data)
            self._cache = settings
            self._write(settings)
            return settings

    def _read(self) -> AgentSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return AgentSettings()
        mode = raw.get("mode") if raw.get("mode") in MODES else MODE_MEAL
        autonomy = raw.get("autonomy")
        if autonomy not in AUTONOMY_LEVELS:
            autonomy = MODES[mode]["autonomy_default"]
        wake_mode = raw.get("wake_mode")
        if wake_mode not in WAKE_MODES:
            wake_mode = WAKE["mode_default"]
        return AgentSettings(
            mode=mode,
            autonomy=autonomy,
            tts_enabled=bool(raw.get("tts_enabled", True)),
            brightness=_clamp(raw.get("brightness"), BRIGHTNESS, BRIGHTNESS["default"]),
            volume=_clamp(raw.get("volume"), VOLUME, VOLUME["default"]),
            speech_rate=_clamp(raw.get("speech_rate"), SPEECH_RATE, SPEECH_RATE["default"]),
            wake_enabled=bool(raw.get("wake_enabled", WAKE["enabled_default"])),
            wake_mode=wake_mode,
            wake_words=tuple(_clean_words(raw.get("wake_words"))),
        )

    def _write(self, settings: AgentSettings) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "mode": settings.mode,
                "autonomy": settings.autonomy,
                "tts_enabled": settings.tts_enabled,
                "brightness": settings.brightness,
                "volume": settings.volume,
                "speech_rate": settings.speech_rate,
                "wake_enabled": settings.wake_enabled,
                "wake_mode": settings.wake_mode,
                "wake_words": list(settings.wake_words),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


agent_settings_store = AgentSettingsStore()


def switch_mode(mode: str) -> AgentSettings:
    """**自动**切换运行模式（用餐开始/结束时用），与"面板里手动切"的区别是：

    手动切模式会把自主互动重置成该模式的默认值（`save(mode=...)` 的行为）；
    自动切必须**保留用户自己选的自主互动级别** —— 否则用户设了"关闭"，
    一开口说"我要吃饭"就被改回"正常"，那是在替用户做决定。
    """
    cur = agent_settings_store.load()      # 注意：load()/save() 各自带锁，这里不能再套一层
    if mode not in MODES or cur.mode == mode:
        return cur
    return agent_settings_store.save(mode=mode, autonomy=cur.autonomy)


def get_settings() -> AgentSettings:
    return agent_settings_store.load()


def tts_enabled() -> bool:
    """会不会出声。关掉后：对话只出文字、主动互动只出表情+文字、唤醒也不再应答语音。"""
    return bool(get_settings().tts_enabled)


def update_settings(**changes) -> AgentSettings:
    return agent_settings_store.save(**changes)


def panel_payload() -> dict:
    """第一页下滑面板所需的全部数据（当前值 + 可选值 + 取值范围）。"""
    settings = get_settings()
    return {
        "settings": settings.public_dict(),
        "modes": [{"key": k, "name": v["name"], "desc": v["desc"]} for k, v in MODES.items()],
        "autonomy_levels": [{"key": k, "name": v["name"], "desc": v["desc"]}
                            for k, v in AUTONOMY_LEVELS.items()],
        "brightness": BRIGHTNESS,
        "volume": VOLUME,
        "speech_rate": SPEECH_RATE,
        "wake_modes": [{"key": k, "name": v["name"], "desc": v["desc"]}
                       for k, v in WAKE_MODES.items()],
        "wake_chunk_ms": WAKE["chunk_ms"],
        "wake_continuous_idle_sec": WAKE["continuous_idle_sec"],
        "wake_continuous_resume_sec": WAKE["continuous_resume_sec"],
        "wake_stop_words": list(WAKE["stop_words"]),
        "default_wake_words": list(DEFAULT_WAKE_WORDS),
    }


def tts_engine_options() -> list[dict]:
    """发声引擎信息（只剩 Qwen3-TTS 一个，仅用于展示当前状态）。"""
    try:
        from ..voice import tts as voice_tts
        return voice_tts.engine_options()
    except Exception:
        return []


# ===================== 语音唤醒：运行时便捷读取 =====================
def wake_enabled() -> bool:
    return bool(get_settings().wake_enabled)


def wake_mode() -> str:
    return get_settings().wake_mode


def wake_words() -> list[str]:
    return list(get_settings().wake_words)


def wake_is_continuous() -> bool:
    return get_settings().wake_mode == WAKE_CONTINUOUS


def is_stop_phrase(text: str) -> bool:
    """连续对话模式：用户说「再见/拜拜/不聊了」等 → 结束本轮连续对话。

    判定与唤醒词一致：清洗标点空格后做子串匹配，四个字以内才允许子串匹配，
    避免长句里偶尔出现「再见」被误判成结束语。
    """
    normalized = re.sub(r"[\s，。！？,.!?、：:;；\-_/\\]+", "", str(text or ""))
    if not normalized or len(normalized) > 12:
        return False
    for word in WAKE["stop_words"]:
        target = re.sub(r"[\s，。！？,.!?、：:;；\-_/\\]+", "", word)
        if target and (normalized == target or target in normalized):
            return True
    return False


def speech_rate_percent() -> int:
    """当前 AI 语速（百分比，供 TTS 使用）。"""
    return get_settings().speech_rate


def tts_engine() -> str:
    """当前发声引擎。只剩 Qwen3-TTS 一个，保留此函数供状态接口/缓存键使用。"""
    try:
        from ..voice import tts as voice_tts
        return voice_tts.active_engine()
    except Exception:
        return "unavailable"


def speech_rate_str() -> str:
    """语速的百分比字符串，例如 '+15%' / '-10%'（Qwen3-TTS 在本地按此重采样时间轴）。"""
    value = speech_rate_percent()
    return f"{'+' if value >= 0 else ''}{value}%"
