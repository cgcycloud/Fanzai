"""进食行为检测：手口追踪（送食/进食速度）、咀嚼统计、分心物、食物残留。

算法 v2（移植自 robot_monitor.py MediaPipeEatingDetector）：
  - 送食：指尖-嘴距离比（除以嘴宽，尺度无关）+ 连续近嘴确认 + 冷却
  - 咀嚼：头动过滤 + 开合度平滑 + 相对变化率 + 连续模式确认（医学标准 15-30 次/分）
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .. import config
from ..core.db import HealthStore

# ===================== 检测参数 =====================
HAND_WINDOW_SEC = 30.0      # 送食统计窗口（秒）
FAST_BITE_THRESHOLD = 12    # 30 秒内超过 12 次判定过快
SLOW_BITE_THRESHOLD = 3     # 少于 3 次判定过慢
CHEW_WINDOW_SEC = 60.0      # 咀嚼统计窗口（次/分）
CHEW_FAST_THRESHOLD = 30.0  # >30 次/分 过快
CHEW_SLOW_THRESHOLD = 15.0  # <15 次/分 过慢
HEAD_MOTION_THRESHOLD = 4.0   # 面部中心偏移比例阈值(%)，超过判定剧烈头动
HEAD_STABLE_COOLDOWN = 0.8    # 头动后需静止冷却(秒)
CHEW_CHANGE_THRESHOLD = 0.18  # 嘴巴开合度相对变化率阈值
CHEW_COOLDOWN = 0.3           # 两次咀嚼最短间隔(秒)
CHEW_PATTERN_THRESHOLD = 3    # 连续满足咀嚼模式的采样数
MOUTH_OPEN_SKIP = 0.02        # 张嘴跳过阈值（占画面高度比例）
BITE_DIST_RATIO_TH = 0.35     # 指尖-嘴距离比阈值
BITE_MIN_CONSEC = 2           # 连续近嘴采样数（1s 采样 × 2 ≈ 2 秒）
BITE_REFRACTORY_SEC = 2.0     # 两次送食最短间隔(秒)
DISTRACT_CONF_THRESH = 0.5
DISTRACT_CLASSES = ["cell phone", "tablet", "laptop"]


# ===================== 数据结构 =====================
@dataclass(frozen=True)
class MotionStats:
    fps: float
    frames: int
    motion_frames: int
    motion_ratio: float


@dataclass
class FoodResidual:
    plate_total_area: float
    food_area: float
    residual_ratio: float   # 0~1
    capture_ts: float


@dataclass
class EatingSpeed:
    bite_count: int
    window_sec: float
    bites_per_min: float
    level: str              # normal / fast / slow / none


@dataclass
class ChewingStat:
    chew_count: int
    window_sec: float
    chews_per_min: float
    level: str              # normal / fast / slow


@dataclass
class DistractStatus:
    has_distract_obj: bool
    object_list: list[str]
    tv_audio_detected: bool


# ===================== 餐桌 ROI 与食物残留 =====================
def create_table_roi_mask(frame: np.ndarray, top_ratio: float | None = None) -> np.ndarray:
    """画面下部（默认 45% 以下）为餐桌检测区。"""
    top_ratio = config.FOOD_RESIDUAL_TOP_RATIO if top_ratio is None else top_ratio
    h, w = frame.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[int(h * top_ratio):h, :] = 255
    return mask


def calc_food_residual(frame: np.ndarray, roi_mask: np.ndarray) -> FoodResidual:
    """餐盘（浅色大轮廓）+ 食物（盘内彩色区域）分割，算剩余占比。"""
    import cv2
    roi_img = cv2.bitwise_and(frame, frame, mask=roi_mask)
    gray = cv2.cvtColor(roi_img, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (11, 11), 0)

    plate_thresh = cv2.threshold(blur, 210, 255, cv2.THRESH_BINARY)[1]
    plate_contours, _ = cv2.findContours(plate_thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    plate_total_area = 0.0
    plate_mask = np.zeros_like(gray)
    for cnt in plate_contours:
        area = cv2.contourArea(cnt)
        if area > 1200:
            plate_total_area += area
            cv2.drawContours(plate_mask, [cnt], -1, 255, -1)

    hsv = cv2.cvtColor(roi_img, cv2.COLOR_RGB2HSV)
    food_mask = cv2.inRange(hsv, np.array([0, 40, 30]), np.array([180, 255, 240]))
    food_mask = cv2.bitwise_and(food_mask, plate_mask)
    food_mask = cv2.morphologyEx(food_mask, cv2.MORPH_CLOSE, np.ones((4, 4), np.uint8))
    food_area = cv2.countNonZero(food_mask)

    residual_ratio = food_area / plate_total_area if plate_total_area > 0 else 0.0
    return FoodResidual(
        plate_total_area=round(plate_total_area, 2),
        food_area=round(food_area, 2),
        residual_ratio=round(residual_ratio, 3),
        capture_ts=time.time(),
    )


# ===================== 手口追踪（MediaPipe） =====================
class HandMouthTracker:
    """送食计数 + 咀嚼统计。兼容 mediapipe solutions / tasks 两种 API，
    landmarks 以「属性访问 .x/.y」的统一形式消费。"""

    def __init__(self):
        import mediapipe as mp
        self.mp_hands = mp.solutions.hands
        self.mp_face = mp.solutions.face_mesh
        self.hands = self.mp_hands.Hands(
            static_image_mode=False, max_num_hands=2,
            min_detection_confidence=0.5, min_tracking_confidence=0.5,
        )
        self.face = self.mp_face.FaceMesh(
            static_image_mode=False, max_num_faces=1, refine_landmarks=True,
        )
        self.bite_records: list[float] = []
        self.last_bite_time = 0.0
        self.consec_near_frames = 0
        self.chew_records: list[float] = []
        self.last_chew_time = 0.0
        self.chew_count = 0
        self.mouth_openness_history: list[float] = []
        self.chewing_pattern_count = 0
        self.face_roi_history: list[tuple[float, float]] = []
        self.last_head_motion_time = 0.0

    @staticmethod
    def _get_mouth_metrics(face_landmarks, h: int, w: int):
        lm = face_landmarks.landmark
        pts = [lm[i] for i in (61, 291, 0, 17)]
        mouth = (sum(p.x for p in pts) / 4, sum(p.y for p in pts) / 4)
        mouth_w = math.hypot((lm[61].x - lm[291].x) * w, (lm[61].y - lm[291].y) * h)
        mouth_scale = max(float(mouth_w), 60.0)
        openness = abs(lm[15].y - lm[13].y) * h
        return mouth, mouth_scale, float(openness)

    def _detect_head_motion(self, face_landmarks, h: int, w: int) -> bool:
        lm = face_landmarks.landmark
        fh = (lm[10].x * w, lm[10].y * h)
        chin = (lm[152].x * w, lm[152].y * h)
        lc = (lm[234].x * w, lm[234].y * h)
        rc = (lm[454].x * w, lm[454].y * h)
        center = ((lc[0] + rc[0]) / 2, (fh[1] + chin[1]) / 2)
        fw = max(abs(rc[0] - lc[0]), 1.0)
        fhgt = max(abs(chin[1] - fh[1]), 1.0)
        self.face_roi_history.append(center)
        if len(self.face_roi_history) < 3:
            return False
        base = self.face_roi_history[-3]
        xs = [abs(c[0] - base[0]) for c in self.face_roi_history[-2:]]
        ys = [abs(c[1] - base[1]) for c in self.face_roi_history[-2:]]
        xr = (sum(xs) / len(xs)) / fw * 100
        yr = (sum(ys) / len(ys)) / fhgt * 100
        if len(self.face_roi_history) > 5:
            self.face_roi_history.pop(0)
        return xr > HEAD_MOTION_THRESHOLD or yr > HEAD_MOTION_THRESHOLD

    def update(self, frame_rgb: np.ndarray) -> Optional[bool]:
        """返回 True=一次送食（bite），None=无人脸/手，False=本帧无送食。"""
        h, w = frame_rgb.shape[:2]
        hand_res = self.hands.process(frame_rgb)
        face_res = self.face.process(frame_rgb)
        if not hand_res.multi_hand_landmarks or not face_res.multi_face_landmarks:
            return None
        face_lms = face_res.multi_face_landmarks[0]
        hands = hand_res.multi_hand_landmarks

        mouth, mouth_scale, openness = self._get_mouth_metrics(face_lms, h, w)
        now = time.time()
        min_ratio = min(
            (math.hypot((hand.landmark[8].x - mouth[0]) * w,
                        (hand.landmark[8].y - mouth[1]) * h) / mouth_scale)
            for hand in hands
        )
        near = min_ratio < BITE_DIST_RATIO_TH
        self.consec_near_frames = self.consec_near_frames + 1 if near else 0
        bite_trigger = False
        if (self.consec_near_frames >= 6
                and (now - self.last_bite_time) >= BITE_REFRACTORY_SEC):
            self.last_bite_time = now
            self.consec_near_frames = 0
            self.bite_records.append(now)
            bite_trigger = True
        self.bite_records = [t for t in self.bite_records if now - t <= HAND_WINDOW_SEC]

        head_motion = self._detect_head_motion(face_lms, h, w)
        if head_motion:
            self.last_head_motion_time = now
            self.chewing_pattern_count = 0
        if (not head_motion) and (now - self.last_head_motion_time) >= HEAD_STABLE_COOLDOWN:
            if openness > max(3.0, h * MOUTH_OPEN_SKIP):
                self.chewing_pattern_count = 0
            else:
                self.mouth_openness_history.append(openness)
                if len(self.mouth_openness_history) > 12:
                    self.mouth_openness_history.pop(0)
                if len(self.mouth_openness_history) >= 5:
                    avg_o = sum(self.mouth_openness_history) / len(self.mouth_openness_history)
                    o_change = abs(openness - avg_o) / avg_o if avg_o > 0 else 0
                    if (now - self.last_chew_time) < CHEW_COOLDOWN:
                        pass
                    elif o_change > CHEW_CHANGE_THRESHOLD:
                        self.chewing_pattern_count += 1
                    else:
                        self.chewing_pattern_count = max(0, self.chewing_pattern_count - 1)
                    if self.chewing_pattern_count >= CHEW_PATTERN_THRESHOLD:
                        self.chew_records.append(now)
                        self.last_chew_time = now
                        self.chew_count += 1
                        self.chewing_pattern_count = 0
        self.chew_records = [t for t in self.chew_records if now - t <= CHEW_WINDOW_SEC]
        return bite_trigger

    def get_speed_stat(self) -> EatingSpeed:
        now = time.time()
        valid = [t for t in self.bite_records if now - t <= HAND_WINDOW_SEC]
        self.bite_records = valid
        bite_cnt = len(valid)
        bpm = bite_cnt / (HAND_WINDOW_SEC / 60)
        if bite_cnt == 0:
            level = "none"
        elif bite_cnt > FAST_BITE_THRESHOLD:
            level = "fast"
        elif bite_cnt < SLOW_BITE_THRESHOLD:
            level = "slow"
        else:
            level = "normal"
        return EatingSpeed(bite_count=bite_cnt, window_sec=HAND_WINDOW_SEC,
                           bites_per_min=round(bpm, 1), level=level)

    def get_chewing_stat(self) -> ChewingStat:
        now = time.time()
        valid = [t for t in self.chew_records if now - t <= CHEW_WINDOW_SEC]
        self.chew_records = valid
        cpm = len(valid) / (CHEW_WINDOW_SEC / 60)
        if cpm > CHEW_FAST_THRESHOLD:
            level = "fast"
        elif cpm < CHEW_SLOW_THRESHOLD:
            level = "slow"
        else:
            level = "normal"
        return ChewingStat(chew_count=len(valid), window_sec=CHEW_WINDOW_SEC,
                           chews_per_min=round(cpm, 1), level=level)


# ===================== 分心物检测（YOLO，可选） =====================
class DistractDetector:
    """YOLO 检测手机/平板/笔记本（缺 ultralytics/模型时不可用，不崩溃）。"""

    def __init__(self, model_path: Path | None = None):
        from ultralytics import YOLO
        path = model_path or (config.PATHS.models_dir / "yolov8n.pt")
        self.model = YOLO(str(path))
        self.conf_thr = DISTRACT_CONF_THRESH
        self.target_classes = DISTRACT_CLASSES

    def detect(self, frame: np.ndarray) -> list[str]:
        res = self.model(frame, conf=self.conf_thr, imgsz=320)[0]
        found = set()
        for box in res.boxes:
            cls_name = self.model.names[int(box.cls)]
            if cls_name in self.target_classes:
                found.add(cls_name)
        return list(found)
