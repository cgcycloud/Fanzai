"""
perception/chewing/detector.py —— 咀嚼速度检测封装（可调用库）

把 perception/chewing/core.py 的底层能力（FaceMesh 人脸关键点 → 肌肉特征 →
咀嚼概率 → 计数/速度）封装成一行可调用的 ChewingDetector 模型：

    from perception import ChewingDetector
    det = ChewingDetector()                       # 自动加载 chewing_model.json（没有则用默认参数）
    info = det.analyze_frame(bgr_frame)           # 返回 dict，见模块底部说明

特性（与 standalone 版一致）：
  - 多肌肉协同判定咀嚼（下巴/下颌角/咬肌/颞肌）
  - 累积计数 chew_count + 60 秒滚动速度 chews_per_min
  - 人脸大幅度移动自动暂停计数（head_moving=True 时不计），HUD 靠 is_moving
  - 无摄像头也能用：对任意 BGR 帧调用即可；mediapipe 未装时 available=False 不崩溃
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import cv2
import numpy as np

from ..config import PATHS
_DEFAULT_CHEW_MODEL = str(PATHS.models_dir / "chewing_model.json")


# 底层引擎来自 perception/chewing/core.py；缺失时降级为不可用（不抛异常）
try:
    from .chewing_engine import (ChewingModel, ChewingMuscleFeatures,
                       ChewingTracker, FaceMeshBackend)
    _ENGINE_OK = True
except Exception:  # pragma: no cover
    ChewingModel = ChewingMuscleFeatures = ChewingTracker = FaceMeshBackend = None
    _ENGINE_OK = False


class ChewingDetector:
    """咀嚼速度检测模型：对每帧 BGR 图返回 咀嚼/计数/速度/头部移动 信息。"""

    def __init__(self, model_path: str = _DEFAULT_CHEW_MODEL,
                 motion_shift: float = 0.18, suppress_sec: float = 0.5):
        self.model_path = model_path
        self.motion_shift = motion_shift
        self.suppress_sec = suppress_sec
        self.available = False
        self.reason = ""
        self.tracker = None
        self._features = None
        self._backend = None

        if not _ENGINE_OK:
            self.reason = "底层引擎不可用（perception.chewing.core 导入失败）"
            return
        try:
            self._backend = FaceMeshBackend()
            if not self._backend.available:
                self.reason = "FaceMesh 不可用（mediapipe 未安装或模型缺失）"
                return
            self._features = ChewingMuscleFeatures()
            model = ChewingModel.load(model_path) or ChewingModel()
            self.tracker = ChewingTracker(model, motion_shift=motion_shift,
                                          suppress_sec=suppress_sec)
            self.available = True
        except Exception as e:  # pragma: no cover
            self.reason = f"初始化失败: {e}"

    @property
    def count(self) -> int:
        return self.tracker.chew_count if self.tracker else 0

    @property
    def is_moving(self) -> bool:
        return bool(self.tracker and self.tracker.is_moving)

    def reset(self) -> None:
        """清空计数与历史（换人/重新开始一餐时调用）"""
        if self.tracker is not None:
            self.tracker.chew_count = 0
            self.tracker.chew_times.clear()
            self.tracker.pattern_count = 0
            self.tracker.history.clear()

    def analyze_frame(self, bgr: np.ndarray, t: Optional[float] = None) -> Dict[str, Any]:
        """输入一帧 BGR，返回：
        {available, face_detected, chewing, probability, chew_count,
         chews_per_min, head_moving, votes}
        """
        if not self.available or self.tracker is None:
            return {"available": False, "reason": self.reason,
                    "face_detected": False, "chewing": False,
                    "probability": 0.0, "chew_count": 0,
                    "chews_per_min": 0, "head_moving": False, "votes": {}}
        try:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            lms = self._backend.detect(rgb)
            feats = self._features.extract(lms)
            t = t if t is not None else time.time()
            fired, prob, votes = self.tracker.update(feats, t)
            return {
                "available": True,
                "face_detected": bool(lms),
                "chewing": bool(fired),
                "probability": float(prob),
                "chew_count": int(self.tracker.chew_count),
                "chews_per_min": int(self.tracker.chews_per_min(t)),
                "head_moving": bool(self.tracker.is_moving),
                "votes": votes,
            }
        except Exception as e:  # pragma: no cover
            return {"available": True, "face_detected": False, "chewing": False,
                    "probability": 0.0, "chew_count": self.tracker.chew_count,
                    "chews_per_min": 0, "head_moving": False,
                    "votes": {}, "error": str(e)}
