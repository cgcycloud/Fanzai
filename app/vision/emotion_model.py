"""
model.py —— 统一表情/情绪识别模型（供外部调用的单一入口）

把「人脸检测 + 情绪分类 + 效价/唤醒度 + 状态词（+ 可选微表情事件）」
整理成 1 个 EmotionModel 对象，其他脚本 import 后直接调用，
不需要关心内部用 FER+ 还是 HSEmotion、模型文件在哪。

    from perception.emotion.model import EmotionModel

    model = EmotionModel(mode="micro")          # micro=FER+8类+微表情模式(默认)，fer=普通8类

    # 1) 摄像头逐帧：
    import cv2
    cap = cv2.VideoCapture(0)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        result = model.analyze_frame(frame)     # 统一返回 dict，见模块底部说明
        for f in result["faces"]:
            print(f["emotion_zh"], f["confidence"], f["state"]["zh"] if f["state"] else "")
            print("  V", f["valence"], "A", f["arousal"])
        # 若 model 开启了 tracker，result["micro_events"] 会给最近一秒的波动事件
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    # 2) 图片：
    result = model.analyze_image("photo.jpg")
    # 或统一入口（自动识别路径 / ndarray）：
    result = model.predict(frame_or_path)

    # 3) 关闭「轻蔑」类误判（默认 True）；想保留就 suppress_contempt=False
    model = EmotionModel(suppress_contempt=False)

返回结构（两个模式统一）：
    {
        "code": 200,
        "face_count": n,
        "engine": "hse_motion" | "ferplus",
        "faces": [
            {
                "bbox": [x, y, w, h],           # 外扩后的人脸框（画框用这个）
                "emotion": "happy",              # 英文小写
                "emotion_zh": "开心",
                "confidence": 0.91,              # 0~1
                "all_emotion_scores": {...},     # 8 类概率
                "valence": 0.81, "arousal": 0.45,    # micro 模式才有，0~1（中性≈0.5）
                "valence_raw": ..., "arousal_raw": ...,  # micro 模式原始值 -1~1
                "state": {"zh": "欣喜", "en": "delighted", "key": "..."} | None,
                "emoji": "😀",
            }, ...
        ],
        "micro_events": [                        # 仅 tracker=True 时出现
            {"kind": "dimension"|"probability", "text": "...", "delta_v": .., "delta_a": .., "duration": ..}
        ],
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np


class EmotionModel:
    """统一的本地表情/情绪识别模型。

    参数：
        mode: "micro"（默认，HSEmotion 8类 + 效价/唤醒度 + 状态词）
              "fer"（FER+ 普通 8 类，只有离散情绪，没有效价/唤醒度）
        suppress_contempt: 屏蔽"轻蔑"类（中性脸常被误判成它），默认 True
        bbox_margin: 人脸框外扩倍数（表情集中在嘴/眉，默认 1.3）
        tracker: 开启微表情事件检测（有状态：内部保留约 1 秒历史），默认 False
        baseline: micro 模式下用个人中性基线做状态判断（先保持中性 1 秒），默认 False

    属性：
        engine:  当前内部引擎对象
        mode / engine_name / labels:  当前模式与标签
    """

    def __init__(self, mode: str = "micro", suppress_contempt: bool = True,
                 bbox_margin: float = 1.3, tracker: bool = False,
                 baseline: bool = False):
        if mode not in ("micro", "fer"):
            raise ValueError("mode 只能是 'micro' 或 'fer'")
        self.mode = mode
        self.suppress_contempt = suppress_contempt
        self.bbox_margin = bbox_margin
        self.tracker_enabled = tracker
        self.baseline = baseline and mode == "micro"

        if self.mode == "fer":
            from .emotion_engine import EmotionEngine
            self.engine = EmotionEngine(suppress_contempt=self.suppress_contempt,
                                        bbox_margin=self.bbox_margin)
            self._micro = False
        else:
            from .emotion_micro import MicroExpressionEngine
            self.engine = MicroExpressionEngine(suppress_contempt=self.suppress_contempt,
                                                bbox_margin=self.bbox_margin)
            self._micro = True

        self._tracker: Optional[Any] = None
        self._mapper: Optional[Any] = None
        if self._micro and self.tracker_enabled:
            from .emotion_micro import MicroTracker
            self._tracker = MicroTracker()
        if self._micro and self.baseline:
            from .state import StateMapper
            self._mapper = StateMapper()

    # ---------- 信息 ----------
    @property
    def engine_name(self) -> str:
        return "hse_motion" if self._micro else "ferplus"

    @property
    def labels(self) -> List[tuple]:
        """当前模式的 (英文小写, 中文) 标签对列表"""
        if self._micro:
            from .emotion_micro import HS_LABELS, HS_LABELS_ZH
            return [(k.lower(), HS_LABELS_ZH[k]) for k in HS_LABELS]
        from .emotion_engine import FER_LABELS, FER_LABELS_ZH
        return list(zip(FER_LABELS, FER_LABELS_ZH))

    def reset_tracker(self) -> None:
        """清空微表情事件的历史窗口（换人/重新开始的时候调用）"""
        if self._tracker is not None:
            self._tracker.history.clear()

    def __repr__(self) -> str:
        return (f"<EmotionModel engine={self.engine_name} "
                f"suppress_contempt={self.suppress_contempt} "
                f"tracker={self.tracker_enabled} baseline={self.baseline}>") 

    # ---------- 核心调用 ----------
    def analyze_frame(self, bgr: np.ndarray) -> Dict[str, Any]:
        """输入一帧 BGR 图像（摄像头帧/图片 ndarray），返回统一结果 dict"""
        raw = self.engine.analyze_frame(bgr)
        faces: List[Dict[str, Any]] = []
        for info in raw["face_list"]:
            f = {
                "bbox": info["bbox"],
                "emotion": info["emotion_label"],
                "emotion_zh": info["emotion_label_zh"],
                "confidence": info["confidence"],
                "all_emotion_scores": info.get("all_emotion_scores", {}),
                "valence": info.get("valence"),
                "arousal": info.get("arousal"),
                "valence_raw": info.get("valence_raw"),
                "arousal_raw": info.get("arousal_raw"),
                "state": None,
                "emoji": info.get("emoji"),
            }
            if self._micro:
                f["state"] = self._map_state(info)
            faces.append(f)

        events: List[Dict[str, Any]] = []
        if self._tracker is not None and faces:
            # 只跟踪第一张脸（多人场景事件会混在一起，取最主要的）
            events = self._tracker.push(raw["face_list"][0])

        return {
            "code": raw.get("code", 200),
            "face_count": raw["face_count"],
            "engine": self.engine_name,
            "faces": faces,
            "micro_events": events,
        }

    def analyze_image(self, src: Union[str, Path, np.ndarray]) -> Dict[str, Any]:
        """输入图片路径或 BGR ndarray → 统一结果（单张图，无 tracker 事件）"""
        result = self.analyze_frame(src) if isinstance(src, np.ndarray) \
            else self._engine_analyze_image_path(src)
        return result

    def predict(self, frame_or_path: Union[str, Path, np.ndarray]) -> Dict[str, Any]:
        """统一入口：传路径 → 分析图片；传 ndarray → 分析帧"""
        if isinstance(frame_or_path, np.ndarray):
            return self.analyze_frame(frame_or_path)
        return self.analyze_image(frame_or_path)

    # ---------- 内部 ----------
    def _map_state(self, info: Dict[str, Any]) -> Optional[Dict[str, str]]:
        try:
            v = float(info.get("valence", 0.5))
            a = float(info.get("arousal", 0.5))
        except (TypeError, ValueError):
            return None
        from .state import map_state
        if self._mapper is not None:
            self._mapper.update(info)
            return self._mapper.map(info)
        return map_state(info["emotion_label"], v, a)

    def _engine_analyze_image_path(self, src: Union[str, Path]) -> Dict[str, Any]:
        import cv2
        bgr = cv2.imread(str(src))
        if bgr is None:
            return {"code": 400, "face_count": 0, "engine": self.engine_name,
                    "faces": [], "micro_events": [], "msg": f"无法读取图片: {src}"}
        return self.analyze_frame(bgr)