"""
engine.py —— 情绪识别：直接用别人训练好的模型
（ONNX Model Zoo 的 Emotion FERPlus 8 类模型，微软 CNTK 训练）

与 mindful_pi/emotion_recognizer.py 的区别：
  * 原实现要自己 train() 一个 ONNX 模型（emotion_light_cnn.onnx），
    且人脸检测依赖 ultralytics(YOLO)；
  * 这里直接用社区/官方发布的预训练模型：
      - 人脸检测：OpenCV Zoo 的 YuNet（face_detection_yunet_2023mar.onnx，232KB；
        OpenCV 5 用 FaceDetectorYN，OpenCV 4 自动回退 Haar 级联）；
      - 情绪识别：emotion-ferplus-8.onnx（34MB, FER+ 数据集, 8 类）。
    两个模型首次运行时自动下载到 data_local/models/。

模型出处（保留版权归原作者）：
  * ONNX Model Zoo: https://github.com/onnx/models/tree/main/validated/vision/body_analysis/emotion_ferplus
  * 论文: Barsoum et al., "Training Deep Networks for Facial Expression
    Recognition with Crowd-Sourced Label Distribution" arXiv:1608.01041
  * 训练源码: https://github.com/ebarsoum/FERPlus

输出 8 类（模型顺序）: neutral happiness surprise sadness anger disgust fear contempt
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from ..config import PATHS


from .emoji_render import EMOTION_TO_EMOJI, FER_LABELS, FER_LABELS_ZH

# ---------------- 常量 ----------------
MODEL_FILENAME = "emotion-ferplus-8.onnx"
MODEL_URL = ("https://github.com/onnx/models/raw/main/validated/"
             "vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx")
MODEL_EXPECTED_MIN_BYTES = 30 * 1024 * 1024  # 34MB 左右

FACE_MODEL_FILENAME = "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                  "face_detection_yunet/face_detection_yunet_2023mar.onnx")
FACE_MODEL_EXPECTED_MIN_BYTES = 100 * 1024  # ~232KB

DEFAULT_MODEL_PATH = PATHS.models_dir / MODEL_FILENAME
DEFAULT_FACE_MODEL_PATH = PATHS.models_dir / FACE_MODEL_FILENAME
CASCADE_RELPATH = "cv2/data/haarcascade_frontalface_default.xml"
INPUT_SIZE = 64
MIN_FACE = 40

_FACE_CACHE_LABELS: List[str] = FER_LABELS


def _stream_download(url: str, tmp: Path, retries: int, label: str) -> None:
    """带重试的流式下载（网络不稳时自动重试）"""
    import time as _time
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mindful-pi-emotion-plugin/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r  {label} 第{attempt}次: {done / 1024 / 1024:.1f}/"
                              f"{total / 1024 / 1024:.1f} MB ({pct}%)", end="", flush=True)
            print()
            return
        except Exception as exc:
            last_err = exc
            print(f"\n  ...第{attempt}次下载失败: {exc}")
            if attempt < retries:
                _time.sleep(2)
    raise RuntimeError(f"下载失败（{retries} 次）：{last_err}\n  来源: {url}")


# ---------------- 下载预训练模型 ----------------
def download_model(target: Optional[Union[str, Path]] = None,
                   url: str = MODEL_URL,
                   force: bool = False,
                   retries: int = 4) -> Path:
    """
    下载预训练 FER+ 情绪分类 ONNX 模型到本地。
    返回模型路径；已存在且大小合理则跳过。
    """
    target = Path(target) if target is not None else DEFAULT_MODEL_PATH
    target = target if target.suffix else target / MODEL_FILENAME
    if target.exists() and not force:
        size = target.stat().st_size
        if size >= MODEL_EXPECTED_MIN_BYTES:
            print(f"[emoji-emotion] 情绪模型已存在: {target} ({size / 1024 / 1024:.1f} MB)")
            return target
        print(f"[emoji-emotion] 情绪模型文件不完整({size} B)，重新下载...")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"[emoji-emotion] 下载预训练情绪模型（~34MB，微软 FER+ 8 类表情识别）...")
    print(f"  {url}")
    _stream_download(url, tmp, retries, "情绪模型")
    tmp.replace(target)
    size = target.stat().st_size
    if size < MODEL_EXPECTED_MIN_BYTES:
        raise RuntimeError(f"情绪模型下载不完整: {size} B ({target})")
    print(f"[emoji-emotion] 情绪模型就绪: {target} ({size / 1024 / 1024:.1f} MB)")
    return target


def download_face_model(target: Optional[Union[str, Path]] = None,
                        force: bool = False,
                        retries: int = 4) -> Path:
    """
    下载预训练人脸检测模型（OpenCV Zoo 的 YuNet，~232KB，Apache-2.0）。
    """
    target = Path(target) if target is not None else DEFAULT_FACE_MODEL_PATH
    if target.exists() and not force:
        size = target.stat().st_size
        if size >= FACE_MODEL_EXPECTED_MIN_BYTES:
            print(f"[emoji-emotion] 人脸检测模型已存在: {target} ({size / 1024:.0f} KB)")
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"[emoji-emotion] 下载预训练人脸检测模型 YuNet（OpenCV Zoo）...")
    _stream_download(FACE_MODEL_URL, tmp, retries, "人脸检测模型")
    tmp.replace(target)
    size = target.stat().st_size
    if size < FACE_MODEL_EXPECTED_MIN_BYTES:
        raise RuntimeError(f"人脸检测模型下载不完整: {size} B ({target})")
    print(f"[emoji-emotion] 人脸检测模型就绪: {target} ({size / 1024:.0f} KB)")
    return target


def download_all(force: bool = False, retries: int = 4) -> None:
    """下载运行所需的全部预训练模型（人脸检测 + 情绪分类）"""
    download_face_model(force=force, retries=retries)
    download_model(force=force, retries=retries)


def softmax(scores: np.ndarray) -> np.ndarray:
    s = scores - np.max(scores)
    e = np.exp(s)
    return e / np.sum(e)


def expand_bbox(x: float, y: float, w: float, h: float, factor: float,
                img_w: int, img_h: int, min_side: int = 40):
    """
    以人脸框中心外扩 factor 倍（如 1.3）并夹到画面内。
    原因：表情在嘴/眉区域，紧密的人脸框常把嘴切掉一截，
    外扩后裁给人脸模型的图包含完整表情，识别明显更稳。
    返回 (x0, y0, x1, y1) 整数，保证边长至少 min_side。
    """
    cx, cy = x + w / 2.0, y + h / 2.0
    nw, nh = w * factor, h * factor
    x0 = max(0, int(cx - nw / 2))
    y0 = max(0, int(cy - nh / 2))
    x1 = min(img_w, int(cx + nw / 2))
    y1 = min(img_h, int(cy + nh / 2))
    if x1 - x0 < min_side:
        if x0 > 0:
            x0 = max(0, x1 - min_side)
        elif x1 < img_w:
            x1 = min(img_w, x0 + min_side)
    if y1 - y0 < min_side:
        if y0 > 0:
            y0 = max(0, y1 - min_side)
        elif y1 < img_h:
            y1 = min(img_h, y0 + min_side)
    return x0, y0, x1, y1


# ---------------- 识别引擎 ----------------
class EmotionEngine:
    """
    本地情绪识别引擎（人脸检测 + FER+ 预训练模型）。
    首次使用会自动下载模型（约 34MB），之后完全离线。
    """

    def __init__(self, model_path: Optional[Union[str, Path]] = None,
                 auto_download: bool = True,
                 min_face: int = MIN_FACE,
                 suppress_contempt: bool = True,
                 bbox_margin: float = 1.3):
        import cv2  # 惰性导入（本机已装 opencv-python）

        self.suppress_contempt = suppress_contempt  # 默认屏蔽"轻蔑"类（中性脸常被误判）
        self.bbox_margin = bbox_margin              # 人脸框外扩系数（嘴/眉别被切掉）

        model_path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        if not model_path.exists():
            if not auto_download:
                raise FileNotFoundError(
                    f"模型不存在: {model_path}，请先运行 python -m perception.emotion.demo download")
            model_path = download_model(model_path)

        # ---- 人脸检测：优先 OpenCV 5 的 YuNet（预训练），OpenCV 4 回退 Haar ----
        self._use_yunet = hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceDetectorYN_create")
        if self._use_yunet:
            face_model = DEFAULT_FACE_MODEL_PATH
            if not face_model.exists():
                if not auto_download:
                    raise FileNotFoundError(
                        f"人脸检测模型不存在: {face_model}，请先运行 python -m perception.emotion.demo download")
                face_model = download_face_model(face_model)
            self._face_det = cv2.FaceDetectorYN_create(str(face_model), "",
                                                       (320, 240), 0.6, 0.3, 10)
        else:  # OpenCV 4.x：Haar 级联
            cascade_file = getattr(cv2, "data", None) and getattr(cv2.data, "haarcascades", None)
            cascade_path = (Path(cascade_file) / "haarcascade_frontalface_default.xml") if cascade_file \
                else PATHS.models_dir / "haarcascade_frontalface_default.xml"
            if not cascade_path.exists():
                raise FileNotFoundError(f"Haar 级联文件缺失: {cascade_path}")
            try:
                self._face_cascade = cv2.CascadeClassifier(str(cascade_path))
                self._face_cascade.load(str(cascade_path))
            except Exception:
                self._face_cascade = None
            if self._face_cascade is None or self._face_cascade.empty():
                raise RuntimeError(f"Haar 级联无法加载: {cascade_path}")

        # ---- 情绪分类：FER+ 预训练 ONNX ----
        import onnxruntime as ort
        self._session = ort.InferenceSession(str(model_path),
                                             providers=["CPUExecutionProvider"])
        self._in_name = self._session.get_inputs()[0].name
        self._out_name = self._session.get_outputs()[0].name
        in_shape = self._session.get_inputs()[0].shape
        self.input_size = int(in_shape[-1]) if in_shape and in_shape[-1] else INPUT_SIZE
        self.model_path = model_path

    # ---------- 人脸检测 ----------
    def detect_faces(self, bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
        if self._use_yunet:
            h, w = bgr.shape[:2]
            self._face_det.setInputSize((w, h))
            ok, faces = self._face_det.detect(bgr)
            out = []
            if ok and faces is not None and len(faces):
                for f in faces:
                    x, y, ww, hh = int(f[0]), int(f[1]), int(f[2]), int(f[3])
                    if ww >= MIN_FACE and hh >= MIN_FACE:
                        out.append((x, y, ww, hh))
            return out
        import cv2
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)  # 光照归一化，提高 Haar 检出率
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(MIN_FACE, MIN_FACE))
        return [tuple(map(int, f)) for f in faces]

    # ---------- 单张人脸分类 ----------
    def _preprocess_face(self, face_bgr: np.ndarray) -> np.ndarray:
        import cv2
        gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (self.input_size, self.input_size),
                          interpolation=cv2.INTER_AREA)
        # 注意：FER+ 模型期望灰度像素原样 [0,255]，不要除以 255！
        # （模型 README 的预处理即 Image.open().resize()，不归一化；
        #   曾误用 /255 导致所有输入都被压到 0~1，输出几乎恒定）
        tensor = gray.astype(np.float32)
        return tensor[np.newaxis, np.newaxis, :, :]       # (1,1,64,64)

    def classify_face(self, face_bgr: np.ndarray) -> Dict:
        tensor = self._preprocess_face(face_bgr)
        logits = self._session.run([self._out_name], {self._in_name: tensor})[0][0]
        probs = softmax(np.asarray(logits, dtype=np.float64).reshape(-1))
        # 屏蔽"轻蔑"：该类别对中性/放松的脸误判率高（日志里老刷屏），
        # 概率清零后重新归一化，让第二高类别（通常是中性/厌恶）顶上
        if self.suppress_contempt and "contempt" in _FACE_CACHE_LABELS:
            ci = _FACE_CACHE_LABELS.index("contempt")
            probs[ci] = 0.0
            probs = probs / probs.sum()
        top = int(np.argmax(probs))
        label = _FACE_CACHE_LABELS[top]
        return {
            "emotion_label": label,                     # 与旧模块同名字段（值为英文）
            "emotion_label_zh": FER_LABELS_ZH[label],   # 中文名
            "emoji": EMOTION_TO_EMOJI[label],           # 对应表情
            "confidence": round(float(probs[top]), 4),
            "all_emotion_scores": {k: round(float(p), 4)
                                   for k, p in zip(_FACE_CACHE_LABELS, probs)},
            "confidences": [round(float(p), 4) for p in probs],
        }

    # ---------- 整帧 / 整图 ----------
    def analyze_frame(self, bgr: np.ndarray) -> Dict:
        """输入 BGR 帧 → {code, face_count, face_list[]}（无日志副作用）"""
        faces = self.detect_faces(bgr)
        face_list = []
        h0, w0 = bgr.shape[:2]
        for (x, y, w, h) in faces:
            x0, y0, x1, y1 = expand_bbox(x, y, w, h, self.bbox_margin, w0, h0)
            if x1 <= x0 or y1 <= y0:
                continue
            face_bgr = bgr[y0:y1, x0:x1]
            info = self.classify_face(face_bgr)
            info["bbox"] = [x0, y0, x1 - x0, y1 - y0]
            info["bbox_raw"] = [int(x), int(y), int(w), int(h)]  # 未外扩的原始框
            face_list.append(info)
        return {"code": 200, "face_count": len(face_list), "face_list": face_list}

    def analyze_image(self, src: Union[str, Path, np.ndarray]) -> Dict:
        """输入图片路径或 BGR ndarray → 分析结果"""
        import cv2
        if isinstance(src, (str, Path)):
            bgr = cv2.imread(str(src))
            if bgr is None:
                return {"code": 400, "msg": f"无法读取图片: {src}", "face_count": 0, "face_list": []}
        else:
            bgr = src
        result = self.analyze_frame(bgr)
        result["msg"] = "ok" if result["face_count"] else "图片未检测到人脸"
        return result

    # ---------- 标注帧（边框 + 英文情绪标签 + 表情缩略图）----------
    def annotate_frame(self, bgr: np.ndarray) -> Tuple[np.ndarray, Dict]:
        import cv2
        from .emoji_render import render_face
        canvas = bgr.copy()
        result = self.analyze_frame(bgr)
        for info in result["face_list"]:
            x, y, w, h = info["bbox"]
            cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 220, 120), 2)
            # 注意：OpenCV putText 不支持中文，这里用英文标签避免乱码
            label = f"{info['emotion_label'].upper()} {info['confidence']:.2f}"
            cv2.putText(canvas, label, (x, max(14, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 120), 2)
            # 表情缩略图贴到框的左下角
            thumb = render_face(info["emoji"], size=48)
            thumb_bgr = cv2.cvtColor(thumb, cv2.COLOR_RGB2BGR)
            T = 48
            x0, y0 = x, y + h + 4
            if y0 + T > canvas.shape[0]:
                y0 = max(0, y - T - 4)
            if x0 + T > canvas.shape[1]:
                x0 = max(0, canvas.shape[1] - T)
            canvas[y0:y0 + T, x0:x0 + T] = thumb_bgr
        return canvas, result

    # ---------- 摄像头实时演示 ----------
    def webcam_loop(self, camera_index: int = 0, window_name: str = "Emotion Demo (q=quit)"):
        import cv2
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 #{camera_index}")
        print("[emoji-emotion] 实时情绪识别中… 按 q 退出")
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("[emoji-emotion] 读取摄像头失败")
                    break
                annotated, result = self.annotate_frame(frame)
                cv2.imshow(window_name, annotated)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
        finally:
            cap.release()
            cv2.destroyAllWindows()


# ---------------- 统一入口（兼容旧模块风格） ----------------
_engine: Optional[EmotionEngine] = None


def get_engine() -> EmotionEngine:
    global _engine
    if _engine is None:
        _engine = EmotionEngine()
    return _engine


def run_emotion_recognition(src: Union[str, Path, np.ndarray]) -> Dict:
    """对外统一接口（见 mindful_pi.emotion_recognizer.run_emotion_recognition 的替代品）"""
    try:
        return get_engine().analyze_image(src)
    except Exception as exc:
        return {"code": 500, "msg": f"表情识别不可用: {exc}", "face_count": 0, "face_list": []}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "data_local/tmp/lena.jpg"
    import json
    res = run_emotion_recognition(name)
    print(json.dumps(res, ensure_ascii=False, indent=2))
