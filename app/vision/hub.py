"""
perception/hub.py —— 统一感知中枢：表情/情绪 + 咀嚼速度 一帧一结果

PerceptionHub 同时持有：
  - EmotionModel（表情/情绪：8类 + 效价/唤醒度 + 状态词 + 微表情事件）
  - ChewingDetector（咀嚼：计数 / 速度 / 头部大位移抑制）
并输出一份 JSON 友好的统一快照，供主程序和前端直接消费。

    from perception import PerceptionHub
    hub = PerceptionHub()
    snap = hub.analyze_frame(bgr_frame)
    hub.write_snapshot("data_local/live_snapshot.json")   # 前端轮询用
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np

from ..config import PATHS

from .chewing import ChewingDetector


def _round(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


class PerceptionHub:
    """统一感知模型：一帧 = {emotion, chewing, ts}。"""

    def __init__(self, emotion_mode: str = "micro",
                 chew_model_path: str = str(PATHS.models_dir / "chewing_model.json"),
                 suppress_contempt: bool = True,
                 bbox_margin: float = 1.3,
                 motion_shift: float = 0.18, suppress_sec: float = 0.5):
        from .emotion_model import EmotionModel
        self.emotion = EmotionModel(mode=emotion_mode,
                                    suppress_contempt=suppress_contempt,
                                    bbox_margin=bbox_margin,
                                    tracker=True)          # 带微表情事件
        self.chewing = ChewingDetector(model_path=chew_model_path,
                                       motion_shift=motion_shift,
                                       suppress_sec=suppress_sec)
        self._last: Optional[Dict[str, Any]] = None
        self._last_write = 0.0

    # ---------- 核心调用 ----------
    def analyze_frame(self, bgr: np.ndarray,
                      t: Optional[float] = None) -> Dict[str, Any]:
        """输入一帧 BGR → 统一快照 dict（JSON 可序列化）"""
        er = self.emotion.analyze_frame(bgr)
        cr = self.chewing.analyze_frame(bgr, t)
        t = t if t is not None else time.time()

        em = {}
        for f in er["faces"]:
            em = {
                "emotion": f["emotion"],
                "emotion_zh": f["emotion_zh"],
                "confidence": _round(f["confidence"]),
                "valence": _round(f["valence"]),
                "arousal": _round(f["arousal"]),
                "state_zh": (f["state"] or {}).get("zh"),
                "state_en": (f["state"] or {}).get("en"),
                "state_key": (f["state"] or {}).get("key"),
            }
            break  # 只取第一张脸（最近的人）
        events = er.get("micro_events", [])
        snap = {
            "ts": _round(t, 4),
            "emotion": {
                "engine": er.get("engine", "hse_motion"),
                "face_count": er["face_count"],
                **em,
                "micro_event": events[-1]["text"] if events else None,
            },
            "chewing": cr,
        }
        self._last = snap
        return snap

    def last_snapshot(self) -> Dict[str, Any]:
        return self._last or {"ts": 0.0, "emotion": {"face_count": 0},
                              "chewing": {"available": self.chewing.available}}

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.last_snapshot(), ensure_ascii=False, indent=indent)

    def write_snapshot(self, path: "str | Path" = None,
                       throttle: float = 0.2) -> bool:
        """把最新快照写盘（节流），供前端/其他进程读取。"""
        path = Path(path) if path else PATHS.data_dir / "live_snapshot.json"
        now = time.time()
        if now - self._last_write < throttle and self._last is not None:
            return False
        self._last_write = now
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(self.to_json(), encoding="utf-8")
            return True
        except Exception:
            return False


def draw_overlay(frame: np.ndarray, snap: Dict[str, Any],
                 x: int = 12, y: int = 12) -> np.ndarray:
    """把快照画到画面左下角（英文文本，cv2 不支持中文）"""
    h, w = frame.shape[:2]
    em = snap.get("emotion", {})
    ch = snap.get("chewing", {})
    lines = []
    if em.get("face_count", 0) > 0:
        lines.append(f"{em.get('state_en','?')} [{em.get('emotion','?')}] "
                     f"c={em.get('confidence',0):.2f} V={em.get('valence',0):.2f} "
                     f"A={em.get('arousal',0):.2f}")
    else:
        lines.append("face: none")
    if ch.get("available"):
        st = "CHEWING" if ch.get("chewing") else "idle"
        mv = "MOVING" if ch.get("head_moving") else "stable"
        lines.append(f"chew {ch.get('chew_count',0)} | {ch.get('chews_per_min',0)}/min "
                     f"| {st} | head:{mv}")
    else:
        lines.append("chew: unavailable")
    x1 = min(w - 12, x + 360)
    cv2.rectangle(frame, (x, y), (x1, y + 24 + 22 * (len(lines) - 1)), (0, 0, 0), -1)
    for i, t in enumerate(lines):
        cv2.putText(frame, t, (x + 8, y + 20 + 22 * i),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 255, 255) if "MOVING" in t or "face: none" in t else (0, 220, 120), 2)
    return frame