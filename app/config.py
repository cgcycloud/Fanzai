"""统一配置 —— 路径平台自适应 + 全部可调参数集中于此。

路径规则：
  树莓派（Linux 且存在 /data）→ 数据 /data，模型优先 /data/models
  Windows / 其它             → 数据 <项目>/data_local，模型 <项目>/models
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """应用根目录：数据（data_local）与模型（models）都相对它定位。

    * 源码运行 → 仓库根目录（`APP_ROOT = <repo>`）
    * PyInstaller 冻结后 → **exe 所在目录**：`__file__` 那时指向临时解包目录
      （`%TEMP%\\_MEIxxxx`，每次启动都不一样、退出即删），拿它当根会导致
      用户数据与模型"重启就没"，所以冻结时必须用 `sys.executable` 的目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


APP_ROOT = app_root()


def detect_raspberry_pi() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if Path("/data").is_dir():
        return True
    try:
        return "Raspberry" in Path("/proc/device-tree/model").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False


IS_PI = detect_raspberry_pi()


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    media_dir: Path
    models_dir: Path
    db_path: Path
    ai_config_path: Path
    ai_key_path: Path

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)


def _resolve_paths() -> Paths:
    if IS_PI:
        data = Path("/data")
        bundled = APP_ROOT / "models"
        models = data / "models" if (data / "models").is_dir() else bundled
        return Paths(
            data_dir=data,
            media_dir=data / "media",
            models_dir=models,
            db_path=data / "user_health.db",
            ai_config_path=data / "ai_config.json",
            ai_key_path=data / "ai_config.key",
        )
    data = APP_ROOT / "data_local"
    return Paths(
        data_dir=data,
        media_dir=data / "media",
        models_dir=APP_ROOT / "models",
        db_path=data / "user_health.db",
        ai_config_path=data / "ai_config.json",
        ai_key_path=data / "ai_config.key",
    )


PATHS = _resolve_paths()


# ===================== 运行期可调（发行版/exe 也能改，不用重新打包）=====================
# 下面几个数常按机器/网络情况调；而 exe/发行包里的 config.py 是打进去的、用户改不了，
# 所以提供两种外部覆盖，优先级：**环境变量 > data_local/tuning.json > 本文件默认值**
#
#   1) data_local/tuning.json            {"memory_send_tokens": 4000, "screen_camera_fps": 20}
#   2) 环境变量（树莓派 systemd 里更方便）  MINDFUL_MEMORY_SEND_TOKENS=4000
#
# 只认白名单里的键，并做了范围收缩；写错或写超范围不会让服务起不来。
TUNING_SPEC: dict[str, tuple[float, float]] = {
    "memory_token_limit": (8_000, 1_000_000),      # 模型窗口（硬上限）
    "memory_send_tokens": (500, 120_000),          # 每次请求真正发出去的量
    "memory_reserve_tokens": (0, 64_000),
    "memory_keep_recent_turns": (2, 64),
    "screen_fps": (2, 60),                         # 表情页渲染帧率
    "screen_camera_fps": (2, 60),                  # 摄像头页渲染帧率
    "screen_idle_fps": (0.5, 30),                  # 没人看时的兜底帧率
    "kws_num_threads": (1, 4),                     # 唤醒解码线程数（1 最省 CPU）
}
_TUNING_APPLIED: dict[str, float] = {}


def _tuning_file() -> dict:
    """data_local/tuning.json（不存在或写坏都当空）。"""
    try:
        import json
        path = PATHS.data_dir / "tuning.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _tuned(name: str, default):
    """取覆盖后的值（环境变量 > tuning.json > 默认值），并记下实际用到的覆盖。"""
    raw = os.environ.get("MINDFUL_" + name.upper(), "")
    if not str(raw).strip():
        raw = _tuning_file().get(name)
    if raw is None or str(raw).strip() == "":
        return default
    lo, hi = TUNING_SPEC[name]
    try:
        value = max(lo, min(hi, float(raw)))
    except (TypeError, ValueError):
        return default
    value = int(value) if isinstance(default, int) else round(value, 2)
    _TUNING_APPLIED[name] = value
    return value


def tuning_applied() -> dict:
    """被外部覆盖掉的值（启动日志与 /api/health 会显示，方便确认"改了到底生效没"）。"""
    return dict(_TUNING_APPLIED)


# ===================== 视觉参数 =====================
VISION_FRAME_FPS = 32            # 流帧率上限（保护值）：采集线程按相机原生帧率抓帧、
                                 # 编码线程事件驱动逐帧发布，相机(30fps)是实际限制；
                                 # 本机 720p 实测交付 ~30fps。树莓派可按 CPU 余量调低。
PERCEPTION_INTERVAL = 0.25       # 感知线程周期（秒）：感知已降采样到 320 宽，18ms/次，不再持续抢 GIL
HAND_MOUTH_INTERVAL = 0.5        # 手口/送食检测周期（秒）：送食动作只有 0.5s 左右，
                                 # 1.2s 采样会漏掉大半，0.5s + 640 宽降采样更稳
AI_VISION_INTERVAL = 10.0        # GLM-4V 视觉分析周期（秒）
MOTION_THRESHOLD = 25            # 运动检测像素差阈值
MOTION_MIN_AREA = 900            # 运动检测最小面积
FOOD_RESIDUAL_TOP_RATIO = 0.45   # 餐盘 ROI 上部占比
VISION_JPEG_WIDTH = 960          # 摄像头 MJPEG 编码宽度（按比例缩放，960 宽比 640 清晰、耗时可忽略）
VISION_JPEG_QUALITY = 72         # 摄像头 MJPEG JPEG 质量（实测 960 宽 q72 编码仅约 2ms）


# ===================== 对话参数 =====================
AI_MAX_TOKENS = 220              # 流式回复 max_tokens（留够空间，太短会把话截成同一副腔调）
SENTENCE_MIN_LEN = 6             # 逗号处提前断句的最小句长（越小首句越早出声）
# 生成等待期**不再播报任何填充语**（旧版会念「让我想想」「嗯，我在听」）。
# 等待改由表情动作表达：前端在做识别/生成时会切到 avatar 的 waiting 动画
# （眼睛绕圈），用户看到"在忙"就够了，不必再占一次 TTS 与一段听觉时间。
TTS_QUEUE_TIMEOUT = 60           # TTS 队列收尾等待（秒）
MEMORY_KEEP_RECENT_TURNS = _tuned("memory_keep_recent_turns", 8)
# 对话上下文预算：当前对话模型（阿里云百炼 qwen3.7-flash）支持 128k 上下文，
# 这里按整窗 128k 配；`usable = LIMIT - RESERVE` 才是"摘要压缩"的触发线
# （留 8k 给系统提示词 + 本轮回复 + 用户这轮的话）。
# ⚠️ 这三个数都可以在 `data_local/tuning.json` 里覆盖（见文件上文"运行期可调"），
# 发行版/exe 用户不用改代码、不用重新打包就能调。
MEMORY_TOKEN_LIMIT = _tuned("memory_token_limit", 128_000)
MEMORY_RESERVE_TOKENS = _tuned("memory_reserve_tokens", 8_000)
# 每次请求**实际发给模型**的上下文上限（= 摘要 + 最近若干轮）。
# 不能把整份记忆都塞进请求：实测同一句话，输入 8.6k tokens 时首字 2.5~4.4s，
# 2k 时 0.7s —— 语音助手的首句延迟主要由 prefill 长度决定。
# 压缩线跟着它走（usable = 本值 × 4，见 memory.usable_tokens），
# 所以更早的对话一定会进摘要，不会出现"没发出去又没进摘要"的记忆空洞。
MEMORY_SEND_TOKENS = _tuned("memory_send_tokens", 2_000)

# 角色预设（"AI 预设"）：只规定身份、风格与专业边界，不写固定话术，
# 避免 AI 照着模板复读同一套句式。
DEFAULT_SYSTEM_PROMPT = (
    "你是“正念饭崽”，一位面向慢病人群（高血压、糖尿病等）的正念饮食陪伴助手。"
    "说话温暖、具体、像老朋友聊天，不要像客服或教科书。\n"
    "表达要求：\n"
    "1) 先回应用户这句话本身，再自然延展；不要答非所问，也不要硬拉回进食话题。\n"
    "2) 不要每轮都用同样的开头和句式（比如反复用“慢慢来”“先深呼吸”“感受一下”开头），"
    "上一轮说过的表达，这一轮换一种说法。\n"
    "3) 一般 1~3 句、不超过 80 字；用户想多聊时可以多说几句。\n"
    "4) 只有确实需要引导进食时，才谈饥饿感、饱足感、咀嚼节奏。\n"
    "专业边界：涉及慢病时说明正念饮食不能替代降糖药或胰岛素，不做诊断、不推荐具体药物；"
    "用户若调整进食方式，建议监测血糖血压并与医生讨论。"
)


# ===================== 语音参数 =====================
# 唤醒与识别全部本地（sherpa-onnx / k2-fsa）：唤醒 KWS → 识别 Paraformer。
# **发声**是云端 Qwen3-TTS-Flash（阿里云百炼，见 app/voice/qwen_tts_engine.py）；
# 旧的 Vosk（识别回退 + 唤醒）已被 sherpa KWS/Paraformer 取代，没有回退代码了；
# 本地 TTS 音色（Kokoro/Matcha/VITS/Piper）与 edge-tts 已全部移除。
SHERPA_MODEL_DIRNAME = "sherpa-onnx-paraformer-zh-small"
SHERPA_NUM_THREADS = 4           # 解码线程数（树莓派建议 2）
# 语音识别期间给视觉让路：暂停感知线程，避免 CPU/GIL 争抢拖慢识别
YIELD_VISION_DURING_ASR = True

# ---- 语音唤醒：sherpa-onnx KWS（zipformer 中文关键词模型）----
# 本模型专为中文唤醒词训练（wenetspeech 1 万小时，3.3M 参数），建模单元是拼音，
# 自定义唤醒词只需把词转成 `n ǐ h ǎo f àn z ǎi @你好饭崽` 一行。
SHERPA_KWS_DIRNAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
SHERPA_KWS_EPOCH = "epoch-12-avg-2"     # 模型包里同时有 epoch-12 / epoch-99 两套
KWS_THRESHOLD = 0.25                    # 命中阈值（调大更严、更少误触发；难唤醒就调小）
KWS_SCORE = 1.0                         # 关键词加成分（调大更容易命中）
# 唤醒流的重置周期（秒）：**必须定期重置**，否则一整天的音频都堆在同一个解码流里，
# 解码越来越慢、CPU 一路涨上去（实测连跑 3 小时从 0.2 核涨到 3.2 核）。
# 只在"当前这段基本安静"时重置，避开说话中间；环境一直很吵时按 2 倍周期兜底重置。
KWS_STREAM_RESET_SEC = 20.0
KWS_STREAM_MAX_SEC = 60.0               # 兜底硬上限（再吵也必须重置一次）
KWS_QUIET_RMS = 0.01                    # 认为"安静"的 RMS 阈值（约 -40dBFS）
KWS_SESSION_TTL_SEC = 900.0             # 会话（浏览器）这么久没喂音频就丢弃，防泄漏
# 唤醒解码线程数。**必须给 1**：这个模型只有 3.3M 参数，2 线程并不会更快，
# 但每片（450ms 音频）实测要烧 ~0.8s CPU（线程池自旋），浏览器持续上传时
# 整机进程会从 0.3 核涨到 1.7~2 核。单线程后解码质量不变、CPU 降一个数量级。
KWS_NUM_THREADS = int(_tuned("kws_num_threads", 1))

ASR_SAMPLE_RATE = 16000
ASR_MAX_RECORD_SECONDS = 8

# ---- 发声：Qwen3-TTS-Flash（阿里云百炼 / DashScope）----
# 端点 / 模型 / 音色 / 语种 / API Key 都存在 data_local/ai_config.json，
# 直接手改即可，改完立即生效（见 app/voice/qwen_tts_engine.py）。
# 下面两个只是**读不到配置时**的兜底默认值。
QWEN_TTS_API_BASE = "https://dashscope.aliyuncs.com/api/v1"   # 北京地域
QWEN_TTS_MODEL = "qwen3-tts-flash"
QWEN_TTS_VOICE = "Cherry"
QWEN_TTS_LANGUAGE = "Chinese"           # 指定语种比 Auto 合成质量更好
QWEN_TTS_TIMEOUT = 30.0                 # 单句合成 + 下载音频的总超时（秒）


# ===================== 分析阈值（analytics）=====================
SPEECH_FAST_THRESH = 140        # 语速过快（字/分钟）→ 焦虑倾向
SPEECH_SLOW_THRESH = 100        # 语速过慢 → 情绪低落倾向
CONTINUOUS_WARN_DAYS = 3        # 连续 N 天触发
WEIGHT_CHANGE_RATIO = 0.05      # 90 天体重波动 5% 预警
WEIGHT_WINDOW_DAYS = 90
EAT_WINDOW_MAX_HOUR = 12        # 每日进食窗口上限
EAT_WINDOW_CONT_DAYS = 3        # 连续 N 天超窗
NIGHT_EAT_HOUR = 21             # 21 点后进食记为宵夜
NIGHT_EAT_WARN_DAYS = 5         # 30 天内宵夜天数预警线

PHASE_CONFIG = {
    "intensive": {"month_range": (0, 3), "interval_days": 14, "name": "强化期"},
    "consolidate": {"month_range": (4, 6), "interval_days": 30, "name": "巩固期"},
    "maintain": {"month_range": (7, 12), "interval_days": 90, "name": "维持期"},
}


# ===================== 硬件参数 =====================
@dataclass(frozen=True)
class AudioConfig:
    device: str = "plughw:seeed2micvoicec,0"   # ReSpeaker
    sample_rate: int = ASR_SAMPLE_RATE
    channels: int = 2
    record_format: str = "S16_LE"


@dataclass(frozen=True)
class ButtonConfig:
    wakeup_gpio: int = 16   # 避开 ReSpeaker 板载按键 GPIO17
    confirm_gpio: int = 22
    skip_gpio: int = 23
    bounce_time: float = 0.08


@dataclass(frozen=True)
class DisplayConfig:
    """GC9A01 圆屏（软件 SPI）"""
    din_gpio: int = 5    # MOSI
    clk_gpio: int = 6    # SCLK
    cs_gpio: int = 24
    dc_gpio: int = 25
    rst_gpio: int = 27
    width: int = 240
    height: int = 240


# ===================== 设备小屏（2.8 寸等）=====================
SCREEN_W = 640                   # 屏幕分辨率（高清横屏 640x480）
SCREEN_H = 480
SCREEN_FPS = int(_tuned("screen_fps", 30))   # 渲染帧率（表情页：元素少，实测 0.8ms/帧）
# 摄像头页要贴一张 960px 的照片并缩放，实测 17ms/帧（+JPEG 2ms）：
# 30fps 就是 0.5 核的"真活"，但配上二十多个线程，GIL 争用会把进程 CPU 放大到 2~3 核
# （实测：浏览器停在摄像头页时整机进程从 0.3 核涨到 2.5 核）。2.8 寸小屏 10fps 足够看。
SCREEN_CAMERA_FPS = int(_tuned("screen_camera_fps", 30))   # 摄像头页（实测每帧 ~19ms → 约 0.57 核；
                                # 树莓派上若卡，把它调回 15 即可，画面观感差别不大）
# 没人拉 MJPEG 流、也没有真实小屏时（纯开发机），把刷新率压下来，别白烧 CPU。
SCREEN_IDLE_FPS = _tuned("screen_idle_fps", 2.5)

# ---- 定期清理（见 core/janitor.py：媒体临时文件 / 日志轮转 / 过期高频事件）----
CACHE_CLEAN_INTERVAL_H = 6.0     # 后台保洁周期（小时）
CACHE_MEDIA_KEEP_HOURS = 24.0    # media/ 下临时音频与图片保留多久
CACHE_LOG_MAX_MB = 2.0           # 日志超过这么大就只保留尾部（保留现场，不无限涨）
CACHE_LOG_KEEP_DAYS = 7.0        # 这么久没写过的 .log 直接删
CACHE_EVENT_KEEP_DAYS = 90.0     # 高频事件（情绪采样/唤醒）保留天数；用餐与对话记录不自动删
SCREEN_JPEG_QUALITY = 74         # 浏览器镜像流的 JPEG 质量
# 输出方式：auto（Linux 有 framebuffer 就用，否则无输出）/ fbdev / spi / none
SCREEN_OUTPUT = "auto"
SCREEN_FBDEV = "/dev/fb1"        # fbtft 驱动的小屏通常映射到 fb1
SCREEN_SPI_CONTROLLER = "st7789" # 直接 SPI 时的控制器：st7789 / ili9341
SCREEN_SPI_DC_GPIO = 25
SCREEN_SPI_RST_GPIO = 27
SCREEN_SPI_OFFSET_X = 0          # 部分屏需要偏移（如 240x320 面板里的 240x240）
SCREEN_SPI_OFFSET_Y = 0
SCREEN_ENABLE = True             # 是否启用设备屏服务（无小屏也会产出 PNG 供网页镜像）
SCREEN_BUTTONS = True            # 树莓派上用 GPIO 跳过键切页（非树莓派自动跳过）


AUDIO_CONFIG = AudioConfig()
BUTTON_CONFIG = ButtonConfig()
DISPLAY_CONFIG = DisplayConfig()
