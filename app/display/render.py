"""设备屏渲染 —— 高清横屏 640×480 @30fps：

  摄像头 / 健康报告（彩色详细，可上下滑动）。表情页由浏览器头像组件负责。
     管理后台同款卡片风格（白卡片 + 绿色强调 + 大数字 KPI）

页面结构：render() 返回 (屏幕图, 内容总高)；内容高 > 屏高时可滚动。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .state import DisplayState, PAGES

# ===================== 主题 =====================
D_BG = (0, 0, 0)
L_BG = (242, 245, 243)
L_CARD = (255, 255, 255)
L_TEXT = (34, 48, 43)
L_MUTED = (124, 138, 132)
L_ACCENT = (47, 158, 110)
L_WARN = (217, 130, 43)
L_LINE = (223, 230, 226)
L_SHADOW = (228, 234, 230)

# 第 3 页与浏览器里的第 3 页同名（浏览器那页是带标签的完整报告，小屏是同一份报告的摘要）
PAGE_TITLES = {"face": "正念饭崽", "camera": "摄像头", "stats": "健康报告"}

EMOTION_ZH = {
    "alert": "警惕", "drowsy": "昏睡", "blessing": "祝福", "anticipation": "期待",
    "anxiety": "焦虑", "surprise": "惊讶", "irritated": "烦躁", "hatred": "憎恨",
    "aggrieved": "委屈", "reassured": "安心", "relaxed": "放松",
    "listening": "倾听", "thinking": "思考", "speaking": "说话", "wink": "眨眼",
    "happy": "放松", "sad": "委屈", "angry": "烦躁", "love": "期待",
    "sleepy": "昏睡", "cry": "委屈", "cool": "放松", "shy": "安心",
    "neutral": "安心", "idle": "待机",
}


def emotion_label(value: object) -> str:
    if value in (None, ""):
        return "—"
    s = str(value)
    return EMOTION_ZH.get(s, s)


# ===================== 字体 =====================
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
]
_font_cache: Dict[int, ImageFont.ImageFont] = {}
_font_path: Optional[str] = None
_font_resolved = False


def _resolve_font() -> Optional[str]:
    global _font_path, _font_resolved
    if _font_resolved:
        return _font_path
    _font_resolved = True
    for group in _FONT_CANDIDATES:
        for p in group:
            if Path(p).exists():
                _font_path = p
                return _font_path
    return None


def _font(size: int) -> ImageFont.ImageFont:
    if size not in _font_cache:
        path = _resolve_font()
        try:
            _font_cache[size] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _ellipsize(d: ImageDraw.ImageDraw, s: str, size: int, max_w: float) -> str:
    if _len(d, s, size) <= max_w:
        return s
    while s and _len(d, s + "…", size) > max_w:
        s = s[:-1]
    return s + "…"


def _len(d: ImageDraw.ImageDraw, s: str, size: int) -> float:
    return d.textlength(s, font=_font(size))


def _text(d: ImageDraw.ImageDraw, xy: Tuple[int, int], s: str, size: int = 16,
          fill=L_TEXT, max_w: Optional[float] = None) -> None:
    if not s:
        return
    if max_w:
        s = _ellipsize(d, s, size, max_w)
    d.text(xy, s, font=_font(size), fill=fill)


# ===================== 通用部件 =====================
def _dots(d: ImageDraw.ImageDraw, x_right: int, y: int, page: str) -> None:
    n = len(PAGES)
    r, gap = 4, 12
    total = n * gap - (gap - r * 2)
    x = x_right - total
    for i, p in enumerate(PAGES):
        cx = x + i * gap
        if p == page:
            d.ellipse((cx, y, cx + r * 2, y + r * 2), fill=L_ACCENT)
        else:
            d.ellipse((cx, y, cx + r * 2, y + r * 2), fill=L_LINE)


def _header_light(d: ImageDraw.ImageDraw, w: int, page: str) -> None:
    d.rectangle((0, 0, w, 40), fill=L_ACCENT)
    _text(d, (14, 8), PAGE_TITLES.get(page, page), 20, (255, 255, 255))
    _dots(d, w - 14, 14, page)


def _card(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    d.rounded_rectangle((x + 2, y + 3, x + w + 2, y + h + 3), radius=12, fill=L_SHADOW)
    d.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=L_CARD)


def _bar(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, ratio: float,
         color=L_ACCENT) -> None:
    d.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=(232, 238, 234))
    inner = int((w - 2) * max(0.0, min(1.0, ratio)))
    if inner > 0:
        d.rounded_rectangle((x + 1, y + 1, x + 1 + inner, y + h - 1), radius=h / 2 - 1, fill=color)


def _kv_chip(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
             title: str, value: str, sub: str = "", color=L_ACCENT) -> None:
    _card(d, x, y, w, h)
    _text(d, (x + 14, y + 10), title, 14, L_MUTED, max_w=w - 28)
    _text(d, (x + 14, y + 30), value, 22, color, max_w=w - 28)
    if sub:
        _text(d, (x + 14, y + h - 24), sub, 12, L_MUTED, max_w=w - 28)


def _scrollbar(d: ImageDraw.ImageDraw, w: int, h: int, scroll: float, content_h: float) -> None:
    if content_h <= h + 1:
        return
    track_h = h - 12
    bar_h = max(36.0, track_h * h / content_h)
    max_scroll = content_h - h
    y = 6 + (track_h - bar_h) * (scroll / max_scroll if max_scroll > 0 else 0)
    d.rounded_rectangle((w - 9, 6, w - 4, h - 6), radius=2.5, fill=(212, 220, 215))
    d.rounded_rectangle((w - 9, y, w - 4, y + bar_h), radius=2.5, fill=L_ACCENT)


# ===================== 摄像头（彩色详细，可滚动）=====================
def _page_camera(st: DisplayState, w: int, h: int, t: float,
                 frame: Optional[Image.Image], analysis: Dict[str, object]) -> Tuple[float, float]:
    header = 40
    content = Image.new("RGB", (w, 780), L_BG)
    d = ImageDraw.Draw(content)
    _header_light(d, w, "camera")

    # 摄像头画面 640×360（原始 16:9，用户反馈"分辨率低"）：下面紧跟一条**两行**的食物
    # 信息条（卡路里/能量/逐条份量），首屏 480px 内既能看画面也能看到热量。
    fy = header + 10
    fh = 360
    d.rounded_rectangle((10, fy - 3, w - 10, fy + fh + 3), radius=12, fill=(16, 22, 19))
    if frame is not None:
        fw2, fh2 = frame.size
        scale = min((w - 26) / fw2, fh / fh2)
        rs = frame.resize((max(1, int(fw2 * scale)), max(1, int(fh2 * scale))))
        content.paste(rs.convert("RGB"), ((w - rs.size[0]) // 2, fy + (fh - rs.size[1]) // 2))
    else:
        _text(d, (w // 2 - 80, fy + fh // 2 - 12), "摄像头未启动", 18, (150, 160, 155))

    # 顺序：食物信息条（卡路里/能量）→ 指标卡 2×2 → 运动/提示
    a = analysis or {}
    m = st.metrics()
    food = a.get("food") or {}
    lvl_map = {"fast": "偏快", "slow": "偏慢", "normal": "正常", "none": "未检测"}

    def lvl_of(key: str) -> str:
        return lvl_map.get(str(m.get(key) or ""), "")

    y = _food_strip(d, w, fy + fh + 10, food)
    cy0 = y + 6
    cw, ch_, gap = (w - 32) // 2, 104, 10

    v = m.get("chews_per_min")
    cnt = m.get("chew_count")
    # 累计次数放在标题行：卡片副标题在默认滚动位置是看不见的
    chew_title = "咀嚼" + (f"（累计 {cnt} 次）" if cnt not in (None, "") else "")
    _kv_chip(d, 12, cy0, cw, ch_, chew_title,
             f"{v} 次/分" if v not in (None, "") else "—",
             (lvl_of("chew_level") or "医学标准 15-30") if v not in (None, "") else "等待数据")
    v = m.get("bites_per_min")
    _kv_chip(d, 12 + cw + gap, cy0, cw, ch_, "进食速度",
             f"{v} 次/分" if v not in (None, "") else "—",
             ("30 秒窗口 · " + lvl_of("eat_level")) if v not in (None, "") else "等待送食")
    if not m.get("vision"):
        face_txt, face_sub = "摄像头已关闭", "日常聊天模式"
    elif m.get("face_count"):
        face_txt, face_sub = f"{m.get('face_count')} 张 · {m.get('emotion') or '-'}", "本地模型实时检测"
    else:
        face_txt, face_sub = "未检测到人脸", "本地模型实时检测"
    _kv_chip(d, 12, cy0 + ch_ + gap, cw, ch_, "人脸情绪", face_txt, face_sub)
    food_txt = str(food.get("description") or food.get("note") or "—")
    kcal = food.get("calorie_estimate_kcal") or food.get("kcal_total")
    _kv_chip(d, 12 + cw + gap, cy0 + ch_ + gap, cw, ch_, "食物（概要）", food_txt,
             f"约 {kcal} kcal" if kcal else "AI 视觉识别", )

    motion = a.get("motion") or {}
    y2 = cy0 + 2 * ch_ + 2 * gap
    d.line((0, y2, w, y2), fill=L_LINE)
    _text(d, (16, y2 + 12),
          f"运动检测：{'有运动' if motion.get('active') else '静止'} · 活动比 {motion.get('motion_ratio', 0)}",
          14, L_MUTED)
    _text(d, (16, y2 + 36), "上下滑动查看全部 · 左右滑动切页", 13, L_MUTED)
    return content, float(y2 + 60)


def _food_strip(d: ImageDraw.ImageDraw, w: int, y: int, food: Dict[str, object]) -> int:
    """食物信息条（两行）：把"识别出的菜品 + 卡路里 + 能量"压进 66px。

    放在摄像头画面正下方，保证 480px 首屏里"看得见画面、也看得见热量"。
    数据来自 `vision/service._analyze_llm` → `_with_nutrition()`：
    `items` 逐条 name/portion_g/kcal，`kcal_total`/`energy_kj` 是合计。
    返回信息条下沿的 y（供后面继续排版）。
    """
    items = food.get("items") if isinstance(food.get("items"), list) else []
    kcal_total = food.get("kcal_total") or food.get("calorie_estimate_kcal")
    kj_total = food.get("energy_kj")
    h = 58
    _card(d, 12, y, w - 24, h)
    _text(d, (26, y + 5), "食物识别", 15, L_MUTED)
    right = []
    if kcal_total:
        right.append(f"合计 约 {kcal_total} kcal")
        if kj_total:
            right.append(f"{kj_total} kJ")
    carb, protein, fat = food.get("carb_g"), food.get("protein_g"), food.get("fat_g")
    if carb is not None and protein is not None and fat is not None:
        right.append(f"碳水{carb:g} 蛋白{protein:g} 脂肪{fat:g}")
    if right:
        _text(d, (w - 26 - _len(d, " · ".join(right), 14), y + 6),
              " · ".join(right), 14, L_ACCENT)
    rows = items[:3]
    if rows:
        line = " · ".join(
            f"{str(it.get('name') or '—')} {it.get('portion_g'):g}g {it.get('kcal')}kcal"
            if it.get("portion_g") else f"{str(it.get('name') or '—')} {it.get('kcal')}kcal"
            for it in rows)
    else:
        line = str(food.get("description") or food.get("note") or "尚未识别到食物（对准餐盘）")
    _text(d, (26, y + 28), line, 14, L_TEXT, max_w=w - 60)
    return y + h


# ===================== ③ 健康报告（彩色详细，可滚动）=====================
def _page_stats(st: DisplayState, w: int, h: int, t: float,
                stats: Dict[str, object]) -> Tuple[float, float]:
    header = 40
    content = Image.new("RGB", (w, 900), L_BG)
    d = ImageDraw.Draw(content)
    _header_light(d, w, "stats")

    wr = (stats.get("week_report") or {}) if stats else {}
    y = header + 12
    cw, gap = (w - 32) // 2, 12

    _card(d, 12, y, cw, 120)
    _text(d, (26, y + 12), "饮食依从性", 14, L_MUTED)
    _text(d, (26, y + 34), str(wr.get("adherence_score") if wr.get("adherence_score") is not None else "—"),
          46, L_ACCENT)
    _text(d, (132, y + 62), "/ 100", 16, L_MUTED)
    trend = {"up": "↑", "down": "↓", "stable": "→"}.get(str(wr.get("weight_trend") or ""), "—")
    _card(d, 12 + cw + gap, y, cw, 120)
    _text(d, (26 + cw + gap, y + 12), "近 4 周", 14, L_MUTED)
    _text(d, (26 + cw + gap, y + 36),
          f"{wr.get('meal_count') if wr.get('meal_count') is not None else '—'} 次用餐", 26, L_TEXT)
    _text(d, (26 + cw + gap, y + 76),
          f"体重 {trend} · 间隔 {wr.get('avg_meal_interval_h') if wr.get('avg_meal_interval_h') is not None else '—'}h",
          14, L_MUTED, max_w=cw - 28)
    y += 132

    ratio = wr.get("emotional_eat_ratio")
    ratio_v = round(float(ratio) * 100, 1) if ratio is not None else None
    _card(d, 12, y, cw, 90)
    _text(d, (26, y + 10), "情绪性进食占比", 14, L_MUTED)
    _text(d, (26, y + 30), f"{ratio_v}%" if ratio_v is not None else "—", 26,
          L_WARN if (ratio_v or 0) > 30 else L_TEXT)
    _bar(d, 26, y + 68, cw - 30, 9, (ratio or 0))
    speech = wr.get("avg_speech_speed")
    _card(d, 12 + cw + gap, y, cw, 90)
    _text(d, (26 + cw + gap, y + 10), "平均语速", 14, L_MUTED)
    _text(d, (26 + cw + gap, y + 30),
          f"{speech} 字/分" if speech is not None else "—", 26, L_TEXT)
    _bar(d, 26 + cw + gap, y + 68, cw - 30, 9, min(1.0, (speech or 0) / 200.0))
    y += 100

    # 情绪分布
    _card(d, 12, y, w - 24, 150)
    _text(d, (26, y + 12), "情绪分布（近 7 天）", 16, L_TEXT)
    dist = stats.get("emotion_distribution") or {}
    yy = y + 46
    if dist:
        items = sorted(dist.items(), key=lambda kv: -kv[1])[:3]
        mx = max(v for _, v in items) or 1
        for name, cnt in items:
            _text(d, (26, yy), emotion_label(name), 15, L_MUTED, max_w=110)
            _bar(d, 140, yy + 3, w - 250, 16, cnt / mx)
            _text(d, (w - 96, yy), str(cnt), 15, L_TEXT)
            yy += 28
    else:
        _text(d, (26, yy), "暂无情绪数据，聊几句就有了", 14, L_MUTED)
        yy += 28
    y += 162

    # 风险预警
    warnings = stats.get("warnings") or []
    _card(d, 12, y, w - 24, 108 if warnings else 72)
    _text(d, (26, y + 12), f"风险预警 {len(warnings)} 条", 16,
          L_TEXT if not warnings else L_WARN)
    wy = y + 42
    for warn in warnings[:2]:
        _text(d, (26, wy), f"[{warn.get('level', '')}] {warn.get('desc', '')}", 13,
              L_WARN, max_w=w - 56)
        wy += 20
    if not warnings:
        _text(d, (26, wy), "暂无饮食、情绪、体重异常风险", 13, L_MUTED)
        wy += 20
    y += 118

    # 复查计划
    rp = stats.get("review_plan") or {}
    _card(d, 12, y, w - 24, 90)
    _text(d, (26, y + 12), "复查计划", 16, L_TEXT)
    _text(d, (26, y + 42),
          f"{rp.get('phase', '—')} · 下次 {rp.get('next_review', '—')}",
          15, L_MUTED, max_w=w - 56)
    y += 102

    _text(d, (16, y + 6), "上下滑动查看全部 · 左右滑动切页", 13, L_MUTED)
    return content, float(content.size[1])


# ===================== 对外接口 =====================
def render(state: DisplayState, t: Optional[float] = None,
           frame: Optional[Image.Image] = None,
           analysis: Optional[Dict[str, object]] = None,
           stats: Optional[Dict[str, object]] = None,
           status: Optional[Dict[str, object]] = None,
           size: Optional[Tuple[int, int]] = None) -> Tuple[Image.Image, float]:
    """渲染当前页 → (屏幕图, 内容总高)。内容高 > 屏高时可滚动。"""
    from ..config import SCREEN_H, SCREEN_W

    w, h = size or (SCREEN_W, SCREEN_H)
    t = t if t is not None else time.time()
    page = state.get_page()
    img = Image.new("RGB", (w, h), D_BG)
    content_h = float(h)

    if page == "face":
        # 表情页由浏览器 avatar 组件负责，服务端不再生成旧 PIL 页面。
        state.set_content_height(page, content_h)
        return img, content_h

    if page == "camera":
        content, content_h = _page_camera(state, w, h, t, frame, analysis or {})
    else:
        content, content_h = _page_stats(state, w, h, t, stats or {})

    scroll = state.get_scroll(page)
    max_scroll = max(0.0, content_h - h)
    scroll = max(0.0, min(max_scroll, scroll))
    img.paste(content.crop((0, int(scroll), w, int(scroll) + h)), (0, 0))
    dd = ImageDraw.Draw(img)
    _scrollbar(dd, w, h, scroll, content_h)

    state.set_content_height(page, content_h)
    return img, content_h
