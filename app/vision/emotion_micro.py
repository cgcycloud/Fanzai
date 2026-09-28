"""
micro.py —— 微表情/轻微表情识别（在 FER+ 基础上增强）

“只有夸张表情才识别得到”的改进思路：
  1. 换成更现代的预训练模型 HSEmotion `enet_b0_8_va_mtl`
     （HSEmotionONNX 官方库模型，Appache-2.0，训练于 AffectNet + VGAF）
     —— 同样是 8 类离散情绪，但多任务输出两个连续维度：
        valence（效价：正/负情绪程度）和 arousal（唤醒度：激动/平静）。
     表情再轻微，这两个连续数值也会跟着微微浮动，
     所以“微表情”可以从连续维度而不是只看离散标签感知。
  2. 时间序列分析（MicroTracker）：跟踪最近 1 秒内的
     情绪概率 / valence / arousal 变化，捕捉快速闪现的情绪波动
     （类似微表情的短暂变化），并记录持续时间与方向。

模型出处：
  * 库: https://github.com/av-savchenko/hsemotion-onnx（HSEmotionONNX）
  * 模型: enet_b0_8_va_mtl.onnx（多任务：8情绪 + valence + arousal）
  * 论文: Savchenko, "Facial expression and attributes recognition based
    on multi-task learning of lightweight neural networks",
    IEEE SIS'21 / 相关 HSEmotion 系列
  * 下载源: https://github.com/HSE-asavchenko/face-emotion-recognition
"""

from __future__ import annotations

# ---- 项目根入 sys.path（脚本直跑 / pytest 均可导入项目包）----
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple, Union

import numpy as np

from ..config import PATHS



from .emoji_render import EMOTION_TO_EMOJI, FER_LABELS_ZH
from .emotion_engine import (DEFAULT_FACE_MODEL_PATH, DEFAULT_MODEL_PATH,
                                 EmotionEngine, expand_bbox)

# ---------------- 常量 ----------------
HS_MODEL_FILENAME = "enet_b0_8_va_mtl.onnx"
HS_MODEL_URL = ("https://github.com/HSE-asavchenko/face-emotion-recognition/raw/main/"
                "models/affectnet_emotions/onnx/enet_b0_8_va_mtl.onnx")
HS_MODEL_EXPECTED_MIN_BYTES = 15 * 1024 * 1024  # ~16MB
DEFAULT_HS_MODEL_PATH = PATHS.models_dir / HS_MODEL_FILENAME

# HSEmotion 8 类（模型输出顺序）
HS_LABELS: List[str] = ["Anger", "Contempt", "Disgust", "Fear",
                        "Happiness", "Neutral", "Sadness", "Surprise"]
HS_LABELS_ZH: Dict[str, str] = {
    "Anger": "愤怒", "Contempt": "轻蔑", "Disgust": "厌恶", "Fear": "恐惧",
    "Happiness": "开心", "Neutral": "中性", "Sadness": "悲伤", "Surprise": "惊讶",
}

IMG_SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def download_hsemotion_model(target: Optional[Union[str, Path]] = None,
                             force: bool = False,
                             retries: int = 6) -> Path:
    """下载 HSEmotion va_mtl 模型（~16MB，带重试）"""
    from .emotion_engine import _stream_download
    target = Path(target) if target is not None else DEFAULT_HS_MODEL_PATH
    if target.exists() and not force:
        size = target.stat().st_size
        if size >= HS_MODEL_EXPECTED_MIN_BYTES:
            print(f"[micro] HSEmotion 模型已存在: {target} ({size / 1024 / 1024:.1f} MB)")
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"[micro] 下载 HSEmotion 多任务模型（8情绪+效价/唤醒度，~16MB）...")
    _stream_download(HS_MODEL_URL, tmp, retries, "HSEmotion")
    tmp.replace(target)
    size = target.stat().st_size
    if size < HS_MODEL_EXPECTED_MIN_BYTES:
        raise RuntimeError(f"HSEmotion 模型下载不完整: {size} B")
    print(f"[micro] HSEmotion 模型就绪: {target} ({size / 1024 / 1024:.1f} MB)")
    return target


class MicroExpressionEngine:
    """
    微表情识别引擎：
      * 人脸检测复用 EmotionEngine（YuNet）；
      * 情绪分类用 HSEmotion va_mtl（8 类 + valence/arousal）。
    """

    def __init__(self, model_path: Optional[Union[str, Path]] = None,
                 auto_download: bool = True,
                 suppress_contempt: bool = True,
                 bbox_margin: float = 1.3):
        # 人脸检测：复用 FER+ 引擎（它只负责出人脸框）
        self._face_engine = EmotionEngine(auto_download=auto_download)
        self.suppress_contempt = suppress_contempt  # 默认屏蔽"轻蔑"类
        self.bbox_margin = bbox_margin              # 人脸框外扩系数（嘴/眉别被切掉）
        model_path = Path(model_path) if model_path else DEFAULT_HS_MODEL_PATH
        if not model_path.exists():
            if not auto_download:
                raise FileNotFoundError(
                    f"HSEmotion 模型不存在: {model_path}，请先运行 detect_emotion.py --micro 或 --hs-download")
            model_path = download_hsemotion_model(model_path)
        import onnxruntime as ort
        self._session = ort.InferenceSession(str(model_path),
                                             providers=["CPUExecutionProvider"])
        self.model_path = model_path

    # ---------- 单张人脸 ----------
    def _preprocess_face(self, face_bgr: np.ndarray) -> np.ndarray:
        import cv2
        x = cv2.resize(face_bgr, (IMG_SIZE, IMG_SIZE)).astype(np.float32) / 255.0
        for i in range(3):
            x[..., i] = (x[..., i] - MEAN[i]) / STD[i]
        return x.transpose(2, 0, 1)[np.newaxis, ...]  # (1,3,224,224) BGR顺序

    def classify_face(self, face_bgr: np.ndarray) -> Dict:
        """返回：情绪 + 置信度 + valence + arousal + 全部分数"""
        import cv2
        if face_bgr.ndim == 2 or face_bgr.shape[2] == 1:
            face_bgr = cv2.cvtColor(face_bgr, cv2.COLOR_GRAY2BGR)
        out = self._session.run(None, {"input": self._preprocess_face(face_bgr)})[0][0]
        out = np.asarray(out, dtype=np.float64)
        emo_logits = out[:-2] if out.shape[0] >= 10 else out
        va = out[-2:] if out.shape[0] >= 10 else np.array([0.5, 0.5])
        # softmax(8 类)
        s = emo_logits - emo_logits.max()
        e = np.exp(s)
        probs = e / e.sum()
        # 屏蔽"轻蔑"：该类别对中性/放松的脸误判率高（日志里老刷屏），
        # 概率清零后重新归一化，让第二高类别（通常是中性/开心）顶上
        if self.suppress_contempt:
            ci = HS_LABELS.index("Contempt")
            probs[ci] = 0.0
            probs = probs / probs.sum()
        top = int(np.argmax(probs))
        label = HS_LABELS[top]
        scores = {k: round(float(p), 4) for k, p in zip(HS_LABELS, probs)}
        # valence/arousal：模型在 ABAW 数据上训练，原始输出为 [-1,1]（中性≈0）。
        # 显示/状态层统一映射到 [0,1]（中性=0.5）：v01 = (raw+1)/2
        raw_va = [float(va[0]), float(va[1])]
        v01 = max(0.0, min(1.0, (raw_va[0] + 1.0) / 2.0))
        a01 = max(0.0, min(1.0, (raw_va[1] + 1.0) / 2.0))
        return {
            "emotion_label": label.lower(),
            "emotion_label_zh": HS_LABELS_ZH[label],
            "emoji": EMOTION_TO_EMOJI[label.lower()],
            "confidence": round(float(probs[top]), 4),
            "all_emotion_scores": scores,
            "valence": v01,               # 效价 0~1（>0.55 积极, <0.45 消极）
            "arousal": a01,               # 唤醒度 0~1（>0.55 激动, <0.45 平静）
            "valence_raw": round(raw_va[0], 4),
            "arousal_raw": round(raw_va[1], 4),
            "ts": time.time(),
        }

    # ---------- 整帧 ----------
    def analyze_frame(self, bgr: np.ndarray) -> Dict:
        faces = self._face_engine.detect_faces(bgr)
        face_list: List[Dict] = []
        h0, w0 = bgr.shape[:2]
        for (x, y, w, h) in faces:
            x0, y0, x1, y1 = expand_bbox(x, y, w, h, self.bbox_margin, w0, h0)
            if x1 <= x0 or y1 <= y0:
                continue
            info = self.classify_face(bgr[y0:y1, x0:x1])
            info["bbox"] = [x0, y0, x1 - x0, y1 - y0]
            info["bbox_raw"] = [int(x), int(y), int(w), int(h)]
            face_list.append(info)
        return {"code": 200, "face_count": len(face_list), "face_list": face_list}

    def analyze_image(self, src: Union[str, Path, np.ndarray]) -> Dict:
        import cv2
        if isinstance(src, (str, Path)):
            bgr = cv2.imread(str(src))
            if bgr is None:
                return {"code": 400, "msg": f"无法读取图片: {src}", "face_count": 0, "face_list": []}
        else:
            bgr = src
        res = self.analyze_frame(bgr)
        res["msg"] = "ok" if res["face_count"] else "图片未检测到人脸"
        return res


# ============================================================
# 时间序列微表情检测
# ============================================================

# valence/arousal 的语义标签
VALENCE_TEXT = [(0.62, "愉悦"), (0.55, "偏愉悦"), (0.45, "中性"),
                (0.35, "偏低落"), (-1.0, "低落")]
AROUSAL_TEXT = [(0.62, "激动/紧张"), (0.55, "偏高"), (0.45, "平稳"),
                (0.35, "偏平静"), (-1.0, "平静")]


def _dim_text(value: float, table: List[Tuple[float, str]]) -> str:
    for thr, txt in table:
        if value >= thr:
            return txt
    return table[-1][1]


class MicroTracker:
    """记录最近 ~1 秒的情绪序列，识别快速闪现的微表情波动。

    带防抖：同一类事件在 cooldown 秒内只报一次（摄像头输出有抖动，
    不加冷却会刷屏）。
    """

    def __init__(self, window: float = 1.0, va_threshold: float = 0.10,
                 prob_threshold: float = 0.20, cooldown: float = 0.9):
        self.window = window
        self.va_threshold = va_threshold
        self.prob_threshold = prob_threshold
        self.cooldown = cooldown
        self.history: Deque[Dict] = deque()
        self._last_event_ts: Dict[str, float] = {}
        # 事件判定：需要连续 min_frames 帧朝同一方向移动，且幅度达标
        self.min_frames = 3

    def push(self, info: Dict) -> List[Dict]:
        """推入一帧结果，返回本帧触发的微表情事件列表（含冷却抑制）"""
        self.history.append(info)
        now = time.time()
        while self.history and now - self.history[0]["ts"] > self.window:
            self.history.popleft()
        events: List[Dict] = []
        if len(self.history) < self.min_frames:
            return events
        base = self.history[-self.min_frames]
        # 1) 连续维度快速变化（微表情的核心信号）
        dv = abs(info["valence"] - base["valence"])
        da = abs(info["arousal"] - base["arousal"])
        if dv >= self.va_threshold or da >= self.va_threshold:
            kind = "dimension"
            if now - self._last_event_ts.get(kind, 0.0) >= self.cooldown:
                self._last_event_ts[kind] = now
                events.append({
                    "kind": kind,
                    "text": f"情绪维度波动: 效价{info['valence']:.2f}({_dim_text(info['valence'], VALENCE_TEXT)}) "
                            f"唤醒{info['arousal']:.2f}({_dim_text(info['arousal'], AROUSAL_TEXT)})",
                    "delta_v": round(dv, 3), "delta_a": round(da, 3),
                    "duration": round(now - base["ts"], 2),
                })
        # 2) 离散标签抖动/概率大跳变
        probs_now = info["all_emotion_scores"]
        probs_base = base["all_emotion_scores"]
        jumps = [(k, abs(probs_now[k] - probs_base[k]))
                 for k in probs_now if abs(probs_now[k] - probs_base[k]) >= self.prob_threshold]
        if jumps:
            jumps.sort(key=lambda kv: -kv[1])
            k, d = jumps[0]
            kind = "probability"
            if now - self._last_event_ts.get(kind, 0.0) >= self.cooldown:
                self._last_event_ts[kind] = now
                events.append({
                    "kind": kind,
                    "text": f"情绪概率跳变: {HS_LABELS_ZH[k.capitalize()] if k.capitalize() in HS_LABELS_ZH else k} "
                            f"±{d:.2f}",
                    "duration": round(now - base["ts"], 2),
                })
        return events

    def latest_va(self) -> Optional[Tuple[float, float]]:
        if not self.history:
            return None
        i = self.history[-1]
        return i["valence"], i["arousal"]


def draw_micro_label(canvas: np.ndarray, info: Dict, x: int, y: int,
                     state_text: Optional[str] = None):
    """在窗口上画 HSEmotion 结果：状态词/情绪 + 效价/唤醒度条
    state_text: 状态词层给的可读词（如"欣喜"），缺省用情绪中文名
    """
    import cv2
    v, a = info["valence"], info["arousal"]
    head = state_text or info["emotion_label_zh"]
    label = f"{head} {info['confidence']:.2f}  V:{v:.2f} A:{a:.2f}"
    cv2.putText(canvas, label, (x, max(14, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 120), 2)
    # 效价条（绿=愉悦方向）
    cv2.putText(canvas, "VAL", (x, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
    cv2.rectangle(canvas, (x + 40, y + 8), (x + 40 + 150, y + 18), (60, 60, 60), -1)
    cv2.rectangle(canvas, (x + 40, y + 8), (x + 40 + int(150 * v), y + 18), (0, 220, 120), -1)
    # 唤醒度条（蓝=激动方向）
    cv2.putText(canvas, "ARO", (x, y + 38), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
    cv2.rectangle(canvas, (x + 40, y + 28), (x + 40 + 150, y + 38), (60, 60, 60), -1)
    cv2.rectangle(canvas, (x + 40, y + 28), (x + 40 + int(150 * a), y + 38), (120, 120, 255), -1)
