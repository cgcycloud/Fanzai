"""
emoji.py —— 新表情库（替代 display.py 的“纯色映射”表情方案）

原实现（mindful_pi/display.py）里 show_emoji() 只是把表情画成一个纯色；
这里改成真正的“像素风表情脸”，每张表情渲染成 240x240 的 RGB 帧，
可直接用于:圆屏(GC9A01)、本地预览(PIL/OpenCV 窗口)、控制台 ASCII 预览。

设计目标：
  1. 表情 = 情绪识别结果（FER+ 8 类）的展示，EMOTION_TO_EMOJI 定义映射；
  2. 兼容原 display.py 的 STTUS 状态名（STATUS_TO_EMOJI），
     以及原 EMOJI_COLORS 里的旧表情键（😊😔🧘🍽️⚠️✅❌💤😌🤗 …）；
  3. 不依赖 PIL/OpenCV 即可渲染（纯 numpy），任何环境都能用。

用法示例：
  from perception.emotion.emoji import render_face, render_ascii, EMOTION_TO_EMOJI
  frame = render_face(EMOTION_TO_EMOJI["happiness"])   # 240x240x3 uint8
  print(render_ascii("😊"))
"""

from __future__ import annotations

import itertools
import math
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

# ============================================================
# 1. 颜色
# ============================================================
SKIN = (255, 224, 189)
SKIN_SAD = (215, 218, 235)      # 悲伤：冷调
SKIN_FEAR = (224, 228, 238)     # 恐惧：苍白
SKIN_ANGER = (250, 206, 200)    # 愤怒：泛红
SKIN_DISGUST = (203, 226, 203)  # 厌恶：泛绿
SKIN_SURPRISE = (255, 240, 218) # 惊讶：偏亮
SKIN_MEDITATE = (229, 222, 250) # 冥想：紫色调
SKIN_EATING = (255, 214, 170)   # 进食：橙色暖调
SKIN_SLEEP = (86, 82, 120)      # 睡眠：深蓝紫（暗）
BLACK = (38, 38, 38)
WHITE = (255, 255, 255)
DARK = (30, 30, 30)
RED = (235, 70, 70)
BLUSH = (255, 150, 150)
TEAR = (120, 180, 255)
GREEN_OK = (70, 190, 90)
YELLOW_WARN = (255, 205, 40)
ORANGE = (255, 165, 0)
PURPLE = (150, 100, 220)

FACE_SIZE = 240  # 与 GC9A01 圆屏一致


# ============================================================
# 2. 情绪(FER+ 8 类) -> 表情 映射
# ============================================================
# 按预训练模型 emotion-ferplus-8 的输出顺序（模型 README 定义）
FER_LABELS: List[str] = ["neutral", "happiness", "surprise", "sadness",
                         "anger", "disgust", "fear", "contempt"]
FER_LABELS_ZH: Dict[str, str] = {
    "neutral": "中性", "happiness": "开心", "surprise": "惊讶",
    "sadness": "悲伤", "anger": "愤怒", "disgust": "厌恶",
    "fear": "恐惧", "contempt": "轻蔑",
}
EMOTION_TO_EMOJI: Dict[str, str] = {
    "neutral": "😐",
    "happiness": "😊",
    "surprise": "😲",
    "sadness": "😔",
    "anger": "😠",
    "disgust": "😖",
    "fear": "😨",
    "contempt": "😏",
}

# 情绪 -> 关键词颜色（兼容原 display.EMOJI_COLORS 的风格）
EMOTION_TO_COLOR: Dict[str, Tuple[int, int, int]] = {
    "neutral": (200, 200, 200),
    "happiness": (0, 220, 120),
    "surprise": (255, 205, 40),
    "sadness": (90, 140, 255),
    "anger": (255, 70, 60),
    "disgust": (120, 190, 90),
    "fear": (170, 170, 230),
    "contempt": (255, 155, 60),
}

# ============================================================
# 3. 机器人状态 -> 表情（兼容 display.py 的 STATUS_COLORS 键名）
# ============================================================
STATUS_TO_EMOJI: Dict[str, str] = {
    "idle": "😐",
    "boot": "😌",
    "wake": "😊",
    "listening": "👂",
    "thinking": "🤔",
    "speaking": "🗣️",
    "error": "❌",
    "sleep": "💤",
    "happy": "😊",
    "sad": "😔",
    "meditate": "🧘",
    "eating": "🍽️",
    "warning": "⚠️",
    "success": "✅",
}

# ============================================================
# 4. 像素绘制小工具（纯 numpy）
# ============================================================


def _grid(size: int):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    return xx, yy


def _fill_circle(canvas: np.ndarray, c: Tuple[int, int], r: float, color: Tuple[int, int, int]):
    xx, yy = _grid(canvas.shape[0])
    mask = (xx - c[0]) ** 2 + (yy - c[1]) ** 2 <= r * r
    canvas[mask] = color


def _fill_ellipse(canvas: np.ndarray, c: Tuple[int, int], rx: float, ry: float,
                  color: Tuple[int, int, int]):
    xx, yy = _grid(canvas.shape[0])
    mask = ((xx - c[0]) / rx) ** 2 + ((yy - c[1]) / ry) ** 2 <= 1.0
    canvas[mask] = color


def _bezier_pts(p0, c, p1, n: int = 48):
    """二次贝塞尔采样点列表"""
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        x = mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0]
        y = mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]
        pts.append((x, y))
    return pts


def _draw_curve(canvas: np.ndarray, pts: List[Tuple[float, float]], thick: float,
                color: Tuple[int, int, int]):
    """把一系列点画成粗曲线（点到线段的距离场）"""
    xx, yy = _grid(canvas.shape[0])
    pts = np.array(pts, dtype=np.float64)
    dist = np.full(xx.shape, np.inf)
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        v = p1 - p0
        L2 = float(v @ v)
        if L2 < 1e-9:
            continue
        t = np.clip(((xx - p0[0]) * v[0] + (yy - p0[1]) * v[1]) / L2, 0.0, 1.0)
        px = p0[0] + t * v[0]
        py = p0[1] + t * v[1]
        d = np.sqrt((xx - px) ** 2 + (yy - py) ** 2)
        dist = np.minimum(dist, d)
    canvas[dist <= thick] = color


def _draw_line(canvas: np.ndarray, p0, p1, thick: float, color: Tuple[int, int, int]):
    _draw_curve(canvas, [tuple(map(float, p0)), tuple(map(float, p1))], thick, color)


def _fill_rect(canvas: np.ndarray, x0: int, y0: int, x1: int, y1: int,
               color: Tuple[int, int, int]):
    canvas[max(0, y0):y1, max(0, x0):x1] = color


# ============================================================
# 5. 表情部件（眼睛 / 眉毛 / 嘴巴）
# ============================================================

_EYE_X = (88, 152)   # 左右眼中心 x
_EYE_Y = 112


def _eye_open(canvas, cx, cy, r=13, pupil=6):
    _fill_circle(canvas, (cx, cy), r, BLACK)
    _fill_circle(canvas, (cx + 4, cy - 4), pupil, WHITE)


def _eye_happy(canvas, cx, cy, r=15):
    # ∪ 形（笑眼）：上拱的弧 => p0 左端、p1 右端，控制点在上方
    _draw_curve(canvas, _bezier_pts((cx - r * 0.8, cy), (cx, cy - r * 0.75), (cx + r * 0.8, cy)), 5, BLACK)


def _eye_closed(canvas, cx, cy, r=13):
    _draw_curve(canvas, _bezier_pts((cx - r * 0.75, cy), (cx, cy - r * 0.25), (cx + r * 0.75, cy)), 5, BLACK)


def _eye_wide(canvas, cx, cy, r=17):
    _fill_circle(canvas, (cx, cy), r, WHITE)
    _fill_circle(canvas, (cx, cy), 9, BLACK)
    _fill_circle(canvas, (cx + 4, cy - 4), 4, WHITE)


def _eye_squint(canvas, cx, cy, r=13):
    _draw_line(canvas, (cx - r * 0.8, cy - 2), (cx + r * 0.8, cy + 3), 5, BLACK)


def _eye_sad(canvas, cx, cy, r=10):
    _fill_circle(canvas, (cx, cy), r, BLACK)
    _fill_circle(canvas, (cx + 3, cy - 3), 5, WHITE)


def _brow_angry(canvas, cx, cy):
    # 眉毛向内下压：左眉 (\ 方向), 右眉 (/ 方向)
    _draw_line(canvas, (cx - 22, cy - 30), (cx + 18, cy - 12), 7, BLACK)
    _draw_line(canvas, (cx + 22, cy - 30), (cx - 18, cy - 12), 7, BLACK)


def _brow_worried(canvas, cx, cy):
    _draw_line(canvas, (cx - 22, cy - 18), (cx + 18, cy - 28), 6, BLACK)
    _draw_line(canvas, (cx + 22, cy - 18), (cx - 18, cy - 28), 6, BLACK)


def _brow_sneer(canvas, cx, cy):
    # 轻蔑：左眉正常平，右眉上挑
    _draw_line(canvas, (cx - 24, cy - 22), (cx + 14, cy - 18), 6, BLACK)
    _draw_line(canvas, (cx + 10, cy - 28), (cx + 34, cy - 34), 6, BLACK)


def _mouth_smile(canvas, mx, my, half=34, dip=26, thick=8):
    _draw_curve(canvas, _bezier_pts((mx - half, my), (mx, my + dip), (mx + half, my)), thick, BLACK)


def _mouth_big_smile(canvas, mx, my, half=36, dip=38, thick=9):
    _draw_curve(canvas, _bezier_pts((mx - half, my), (mx, my + dip), (mx + half, my)), thick, BLACK)
    _fill_ellipse(canvas, (mx, my + dip * 0.55), half * 0.62, dip * 0.42, (210, 90, 80))  # 舌头


def _mouth_frown(canvas, mx, my, half=32, rise=26, thick=8):
    _draw_curve(canvas, _bezier_pts((mx - half, my), (mx, my - rise), (mx + half, my)), thick, BLACK)


def _mouth_flat(canvas, mx, my, half=26, thick=7):
    _draw_line(canvas, (mx - half, my), (mx + half, my), thick, BLACK)


def _mouth_open(canvas, mx, my, rx=20, ry=26):
    _fill_ellipse(canvas, (mx, my + 6), rx, ry, (90, 40, 40))
    _fill_ellipse(canvas, (mx, my + 14), rx * 0.7, ry * 0.45, (235, 130, 120))  # 舌头


def _mouth_wavy(canvas, mx, my, half=30, amp=9, thick=7):
    pts = []
    n = 8
    for i in range(n + 1):
        t = i / n
        x = mx - half + 2 * half * t
        y = my + amp * math.sin(t * math.pi * 2.6) * (1 if 0 < t < 1 else 0)
        pts.append((x, y))
    _draw_curve(canvas, pts, thick, BLACK)


def _mouth_smirk(canvas, mx, my, half=34, thick=8):
    # 一侧上扬：左低右高
    p0 = (mx - half, my + 8)
    p1 = (mx + half, my - 8)
    c = (mx, my + 6)
    _draw_curve(canvas, _bezier_pts(p0, c, p1), thick, BLACK)


def _mouth_o(canvas, mx, my, r=14):
    _fill_circle(canvas, (mx, my + 4), r, (110, 50, 50))


# ============================================================
# 6. 表情脸规格（emoji -> 各部件的参数）
# ============================================================

# 字段：face 肤色, eyes 眼睛, brows 眉毛, mouth 嘴巴, extra 附加
_FACE_SPECS: Dict[str, dict] = {
    "😐": dict(face=SKIN, eyes="open", mouth="flat"),
    "😊": dict(face=SKIN, eyes="happy", mouth="big_smile", extra="blush"),
    "😲": dict(face=SKIN_SURPRISE, eyes="wide", mouth="open", extra="no_brow"),
    "😔": dict(face=SKIN_SAD, eyes="sad", brows="worried", mouth="frown", extra="tear"),
    "😠": dict(face=SKIN_ANGER, eyes="open", brows="angry", mouth="frown"),
    "😖": dict(face=SKIN_DISGUST, eyes="squint", mouth="wavy"),
    "😨": dict(face=SKIN_FEAR, eyes="wide", brows="worried", mouth="wavy"),
    "😏": dict(face=SKIN, eyes="open", brows="sneer", mouth="smirk"),
    "🧘": dict(face=SKIN_MEDITATE, eyes="closed", mouth="smile"),
    "🍽️": dict(face=SKIN_EATING, eyes="happy", mouth="open"),
    "💤": dict(face=SKIN_SLEEP, eyes="closed", mouth="small_smile", extra="zzz"),
    "🤔": dict(face=SKIN, eyes="squint", mouth="wavy", extra="no_brow"),
    "😌": dict(face=SKIN, eyes="closed", mouth="smile", extra="blush"),
    "🤗": dict(face=SKIN, eyes="closed", mouth="big_smile", extra="hug"),
    "👂": dict(face=SKIN, eyes="open", mouth="small_smile", extra="ears"),
    "🗣️": dict(face=SKIN, eyes="open", mouth="open", extra="no_brow"),
    "⚠️": dict(symbol="warn"),
    "✅": dict(symbol="ok"),
    "❌": dict(symbol="no"),
}

# 全部表情键（有序，便于展示）
ALL_EMOJIS: List[str] = list(_FACE_SPECS.keys())


def render_face(emoji: str, size: int = FACE_SIZE) -> np.ndarray:
    """渲染一张表情脸/符号 → (size, size, 3) uint8 RGB 帧"""
    spec = _FACE_SPECS.get(emoji)
    if spec is None:
        raise KeyError(f"未知表情: {emoji}（可用: {ALL_EMOJIS}）")
    canvas = np.full((size, size, 3), 22, dtype=np.uint8)  # 深灰背景

    scale = size / FACE_SIZE
    def s(v):  # 坐标缩放
        return int(round(v * scale))
    def cs(v):  # 半径/粗细缩放（保留小数）
        return v * scale

    if spec.get("symbol"):
        c = s(120)
        r = s(96)
        if emoji == "⚠️":
            _fill_circle(canvas, (c, c), r, (60, 55, 40))
            tri = [(c, s(58)), (c + s(62), s(178)), (c - s(62), s(178))]
            _fill_triangle(canvas, tri, YELLOW_WARN)
            _fill_rect(canvas, c - s(10), s(96), c + s(10), s(138), DARK)
            _fill_circle(canvas, (c, s(158)), s(9), DARK)
        elif emoji == "✅":
            _fill_circle(canvas, (c, c), r, GREEN_OK)
            p0, p1, p2 = (c - s(52), s(118)), (c - s(16), s(156)), (c + s(60), s(78))
            _draw_line(canvas, p0, p1, cs(14), WHITE)
            _draw_line(canvas, p1, p2, cs(14), WHITE)
        elif emoji == "❌":
            _fill_circle(canvas, (c, c), r, RED)
            _draw_line(canvas, (c - s(52), s(76)), (c + s(52), s(164)), cs(16), WHITE)
            _draw_line(canvas, (c + s(52), s(76)), (c - s(52), s(164)), cs(16), WHITE)
        return canvas

    # ---- 脸 ----
    face_color = spec["face"]
    if emoji == "💤":
        _fill_circle(canvas, (s(120), s(133)), cs(88), face_color)   # 暗色圆脸
    else:
        _fill_circle(canvas, (s(120), s(132)), cs(94), face_color)

    if spec.get("extra") == "hug":
        # 两侧小手臂
        _draw_curve(canvas, _bezier_pts((s(30), s(120)), (s(48), s(180)), (s(88), s(196))), cs(14), face_color)
        _draw_curve(canvas, _bezier_pts((s(210), s(120)), (s(192), s(180)), (s(152), s(196))), cs(14), face_color)

    if spec.get("extra") == "ears":
        _fill_circle(canvas, (s(30), s(128)), cs(20), face_color)
        _fill_circle(canvas, (s(210), s(128)), cs(20), face_color)
        _fill_circle(canvas, (s(30), s(128)), cs(9), (180, 150, 130))
        _fill_circle(canvas, (s(210), s(128)), cs(9), (180, 150, 130))

    # ---- 五官 ----
    eyes = spec.get("eyes", "open")
    for cx in _EYE_X:
        e = _eye_open
        if eyes == "happy":
            e = _eye_happy
        elif eyes == "closed":
            e = _eye_closed
        elif eyes == "wide":
            e = _eye_wide
        elif eyes == "squint":
            e = _eye_squint
        elif eyes == "sad":
            e = _eye_sad
        e(canvas, s(cx), s(_EYE_Y), cs(13))

    brows = spec.get("brows")
    if brows == "angry":
        for cx in _EYE_X:
            _brow_angry(canvas, s(cx), s(_EYE_Y))
    elif brows == "worried":
        for cx in _EYE_X:
            _brow_worried(canvas, s(cx), s(_EYE_Y))
    elif brows == "sneer":
        for cx in _EYE_X:
            _brow_sneer(canvas, s(cx), s(_EYE_Y))

    mouth = spec.get("mouth", "flat")
    mx, my = s(120), s(168)
    if mouth == "smile":
        _mouth_smile(canvas, mx, my, cs(32), cs(24))
    elif mouth == "big_smile":
        _mouth_big_smile(canvas, mx, my, cs(34), cs(34))
    elif mouth == "small_smile":
        _mouth_smile(canvas, mx, my, cs(24), cs(14), cs(7))
    elif mouth == "frown":
        _mouth_frown(canvas, mx, my, cs(30), cs(24))
    elif mouth == "flat":
        _mouth_flat(canvas, mx, my, cs(26))
    elif mouth == "open":
        _mouth_open(canvas, mx, my, cs(18), cs(24))
    elif mouth == "wavy":
        _mouth_wavy(canvas, mx, my, cs(28), cs(9))
    elif mouth == "smirk":
        _mouth_smirk(canvas, mx, my, cs(32))
    elif mouth == "o":
        _mouth_o(canvas, mx, my, cs(13))

    extra = spec.get("extra")
    if extra == "blush":
        _fill_circle(canvas, (s(64), s(152)), cs(14), BLUSH)
        _fill_circle(canvas, (s(176), s(152)), cs(14), BLUSH)
    elif extra == "tear":
        _fill_circle(canvas, (s(176), s(140)), cs(7), TEAR)
        _fill_circle(canvas, (s(182), s(152)), cs(5), TEAR)
        _fill_circle(canvas, (s(186), s(163)), cs(4), TEAR)
    elif extra == "zzz":
        _draw_curve(canvas, _bezier_pts((s(178), s(58)), (s(192), s(74)), (s(168), s(92))), cs(5), WHITE)
        _draw_curve(canvas, _bezier_pts((s(190), s(38)), (s(204), s(52)), (s(182), s(70))), cs(4), WHITE)
        _draw_curve(canvas, _bezier_pts((s(200), s(20)), (s(212), s(32)), (s(194), s(48))), cs(3), WHITE)
    return canvas


def _fill_triangle(canvas: np.ndarray, pts, color: Tuple[int, int, int]):
    """填充三角形（3 个 (x,y) 点）"""
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    for y in range(max(0, y0), min(canvas.shape[0], y1 + 1)):
        edges = []
        for i in range(3):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % 3]
            if ay == by:
                continue
            if (ay <= y < by) or (by <= y < ay):
                x = ax + (y - ay) * (bx - ax) / (by - ay)
                edges.append(x)
        if len(edges) >= 2:
            lo, hi = int(min(edges)), int(math.ceil(max(edges)))
            canvas[y, max(0, lo):hi] = color


# ============================================================
# 7. 输出：ASCII 预览 / 拼图预览 / 保存
# ============================================================

_ASCII_RAMP = " .:-=+*#%@"


def render_ascii(emoji: str, cols: int = 30) -> str:
    """把表情降采样成 ASCII 字符画（用于控制台预览，无需任何图形库）"""
    frame = render_face(emoji, size=240)
    gray = frame.mean(axis=2).astype(np.float64) / 255.0
    rows = max(4, cols * 2 // 3)
    out = []
    for r in range(rows):
        y0 = int(r * 240 / rows)
        y1 = int((r + 1) * 240 / rows) or 240
        line = []
        for c in range(cols):
            x0 = int(c * 240 / cols)
            x1 = int((c + 1) * 240 / cols) or 240
            block = gray[y0:y1, x0:x1]
            v = float(block.mean()) if block.size else 0.0
            line.append(_ASCII_RAMP[min(len(_ASCII_RAMP) - 1, int(v * len(_ASCII_RAMP)))])
        out.append("".join(line))
    return "\n".join(out)


def preview_all(size: int = 120, cols: int = 5) -> Tuple[np.ndarray, str]:
    """拼一张所有表情的预览图 + ASCII 对照表文字"""
    rows = (len(ALL_EMOJIS) + cols - 1) // cols
    pad = 12
    tile = np.full((rows * (size + pad) + pad, cols * (size + pad) + pad, 3), 16, dtype=np.uint8)
    for idx, emoji in enumerate(ALL_EMOJIS):
        r, c = divmod(idx, cols)
        y0 = pad + r * (size + pad)
        x0 = pad + c * (size + pad)
        tile[y0:y0 + size, x0:x0 + size] = render_face(emoji, size)
    lines = []
    for i in range(0, len(ALL_EMOJIS), cols):
        chunk = ALL_EMOJIS[i:i + cols]
        lines.append("   ".join(f"{e}" for e in chunk))
    return tile, "\n".join(lines)


def save_preview(path: str, size: int = 120, cols: int = 5):
    """把全部表情保存为一张 PNG"""
    tile, _ = preview_all(size=size, cols=cols)
    _save_image(tile, path)


def _save_image(frame_rgb: np.ndarray, path: str):
    try:
        from PIL import Image
        Image.fromarray(frame_rgb).save(path)
    except Exception:
        import cv2
        cv2.imwrite(path, cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))


# ============================================================
# 8. 可选：直接上圆屏 GC9A01（不修改原驱动，调用其 command/write）
# ============================================================

def write_to_lcd(emoji: str, lcd) -> bool:
    """
    把表情帧推送到 GC9A01 圆屏。
    lcd 为 mindful_pi.drivers.gc9a01.GC9A01 实例（需要 lgpio + 真机），
    Windows 本地没有真屏时不要调用。
    """
    frame = render_face(emoji, FACE_SIZE)  # RGB uint8
    try:
        lcd.command(0x2A, (0, 0, 0, FACE_SIZE - 1))
        lcd.command(0x2B, (0, 0, 0, FACE_SIZE - 1))
        pixels = []
        for y in range(FACE_SIZE):
            for x in range(FACE_SIZE):
                r, g, b = frame[y, x]
                val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                pixels.append(val >> 8)
                pixels.append(val & 0xFF)
        lcd.command(0x2C)
        lcd.write(bytes(pixels))
        return True
    except Exception:
        return False