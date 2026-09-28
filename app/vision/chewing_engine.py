# -*- coding: utf-8 -*-
"""
独立咀嚼检测器：基于 MediaPipe 468/478 面部关键点的多肌肉协同模型
====================================================================

设计思路（与"多肌肉共同投票判定咀嚼"一致）：
    不把眨眼、抬眉等当作并列的独立检测，而是把多个**咀嚼相关肌肉证据**
    一起投票，判定「是否正在咀嚼」这一个动作。

    主信号  ：下巴最低点(175) 垂直位移      —— 下颌开合，权重最高
    协同 1  ：双侧下颌角(234/454) 横向距离   —— 咀嚼时的左右微摆
    协同 2  ：咬肌 ROI（腮帮子 4 点×2 侧）   —— 咬合阶段向外扩张（面积脉动）
    协同 3  ：颞肌 ROI（太阳穴 2 点×2 侧）   —— 用力咬合凸起（辅助）
    过滤器 ：唇开度（13/14）                 —— 说话/哈欠嘴大张，直接否决
    防误计  ：人脸大幅移动抑制               —— 转头/俯仰/远离镜头等大幅位移时，
                                              暂停咀嚼计数（双眼中点位移/脸宽 > 阈值
                                              即触发，抑制期默认 0.5s 后自动恢复）

训练方式  ：按键标注。按 C 键时记录按键前后 ±WINDOW_SEC 秒的**连续视频帧**，
            从这些正样本帧中统计各肌肉特征的「活动幅度」，对照静止基线噪声
            自动生成个性化阈值；检测时逐帧滚动窗口计算活度并加权投票。

所有几何量均使用**归一化关键点坐标**（x,y ∈ [0,1]），与摄像头距离无关，
标定环境与使用环境距离不同也无需重标。

运行方式：
    python detect_chewing.py
    菜单：1 交互标定（保存 chewing_model.json）
          2 实时咀嚼检测（默认参数 / 已标定参数）
          3 无摄像头模拟演示（验证算法流程）
"""

import os
import sys
import json
import time
from collections import deque
from pathlib import Path
from typing import List, Optional
from ..config import PATHS
_MODELS_DIR = str(PATHS.models_dir)
# 训练产物/标定进度都放在项目数据目录，避免写到当前工作目录后"训练了却不生效"
_DEFAULT_MODEL_PATH = str(PATHS.models_dir / "chewing_model.json")
_DEFAULT_SESSION_PATH = str(PATHS.data_dir / "chewing_calibration_session.json")

import cv2
import numpy as np


# ---------- 控制台输出 ----------
# 注意：**不要**把 stdout 重设成 cp936。Windows 控制台的底层缓冲要求 UTF-8 字节
# （它自己转 UTF-16 输出），改成 GBK 后写出的字节会被按 UTF-8 解 → 中文全乱码。
# 统一由 run.bat 设置 chcp 65001 + PYTHONUTF8=1 即可。


def _safe(s):
    try:
        return str(s)
    except Exception:
        return repr(s)


def p_info(s):
    print(f"[INFO] {s}")


def p_ok(s):
    print(f"[OK] {s}")


def p_warn(s):
    print(f"[WARN] {s}")


def p_err(s):
    print(f"[ERR] {s}")


# =====================================================================
#  1) MediaPipe FaceMesh 双后端（solutions 旧版 / tasks 新版）
# =====================================================================

class FaceMeshBackend:
    """封装 FaceMesh 检测：旧版 solutions 或新版 tasks API 自动回退。

    统一返回「478 个归一化关键点」列表 [(x, y), ...]，468 核心点全部包含。
    """

    MODEL_NAMES = {
        "tasks": {
            "file": "face_landmarker.task",
            "url": ("https://storage.googleapis.com/mediapipe-models/"
                    "face_landmarker/face_landmarker/float16/1/"
                    "face_landmarker.task"),
        }
    }

    def __init__(self, model_dir: str = _MODELS_DIR):
        self.backend = None          # "solutions" | "tasks" | None
        self.mp = None
        self._face_mesh = None
        self._landmarker = None
        self.model_dir = Path(model_dir)
        self.available = False
        self._init_solutions() or self._init_tasks()

    # ---- 旧版 solutions ----
    def _init_solutions(self) -> bool:
        try:
            import mediapipe as mp
            if not hasattr(mp, "solutions"):
                return False
            from mediapipe import solutions
            self.mp = mp
            self._face_mesh = solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self.backend = "solutions"
            self.available = True
            return True
        except Exception:
            return False

    # ---- 新版 tasks ----
    def _init_tasks(self) -> bool:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision
            model = self._ensure_model()
            if model is None:
                return False
            options = vision.FaceLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_path=str(model)),
                num_faces=1,
                running_mode=vision.RunningMode.VIDEO,
            )
            self._landmarker = vision.FaceLandmarker.create_from_options(options)
            self.mp = mp
            self.backend = "tasks"
            self.available = True
            return True
        except Exception as e:
            p_warn(f"tasks FaceLandmarker 初始化失败: {e}")
            return False

    def _ensure_model(self):
        import urllib.request

        spec = self.MODEL_NAMES["tasks"]
        for d in (self.model_dir, Path("/data/models")):
            p = d / spec["file"]
            if p.exists():
                return p
        try:
            dest = self.model_dir / spec["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            p_info(f"下载 MediaPipe 模型: {spec['file']} ...")
            urllib.request.urlretrieve(spec["url"], dest)
            return dest
        except Exception as e:
            p_warn(f"模型下载失败: {e}")
            return None

    # ---- 检测：返回 [(x, y), ...] 或 None ----
    def detect(self, rgb) -> list:
        """输入 RGB 帧，返回 478 个归一化关键点列表；未检测到返回 None"""
        try:
            if self.backend == "tasks":
                img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
                result = self._landmarker.detect_for_video(img, int(time.time() * 1000))
                if not result.face_landmarks:
                    return None
                lms = result.face_landmarks[0]
                return [(lm.x, lm.y) for lm in lms]
            if self.backend == "solutions":
                result = self._face_mesh.process(rgb)
                if not result.multi_face_landmarks:
                    return None
                lms = result.multi_face_landmarks[0].landmark
                return [(lm.x, lm.y) for lm in lms]
        except Exception as e:
            p_err(f"FaceMesh 检测异常: {e}")
        return None


# =====================================================================
#  2) 咀嚼肌肉特征（468 点位方案，全部使用归一化坐标，距离无关）
# =====================================================================

class ChewingMuscleFeatures:
    """从 478 关键点中提取咀嚼相关肌肉特征。

    点位（MediaPipe 468 核心点编号，478 版本完全兼容）：
      下巴最低点 menton   : 175
      左/右下颌角          : 234 / 454
      左侧咬肌 ROI         : 148, 176, 192, 214
      右侧咬肌 ROI         : 374, 400, 416, 436
      左侧颞肌 ROI         : 109, 127
      右侧颞肌 ROI         : 338, 356
      上/下唇中点          : 13 / 14
    """

    # 点位分组
    CHIN = 175                 # 下巴最低点（主信号）
    JAW_L, JAW_R = 234, 454    # 左右下颌角
    MASSETER_L = [148, 176, 192, 214]
    MASSETER_R = [374, 400, 416, 436]
    TEMPORALIS_L = [109, 127]
    TEMPORALIS_R = [338, 356]
    LIP_TOP, LIP_BOTTOM = 13, 14
    # 脸部恒定参考：双眼外角（咀嚼时基本不动，作尺度基准）
    EYE_L, EYE_R = 33, 263

    def __init__(self):
        self.max_pt = 477       # 478 点版本索引上限（0~477）
        # 参考尺寸下限：双眼距离过小（脸太小/检测退化）则本帧不作数
        self.min_face_ref = 0.02

    # ---- 几何工具 ----
    @staticmethod
    def _polygon_area(pts):
        """多边形面积（shoelace），输入归一化坐标列表 → 面积无量纲"""
        if len(pts) < 3:
            return 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return 0.5 * abs(sum(xs[i] * ys[(i + 1) % len(pts)] -
                            xs[(i + 1) % len(pts)] * ys[i] for i in range(len(pts))))

    def _safe_pts(self, lms, ids):
        out = []
        for i in ids:
            if 0 <= i < len(lms):
                out.append(lms[i])
        return out

    # ---- 特征提取（单帧）：全部除以脸部参考尺寸 → 比例值 ----
    def extract(self, lms) -> dict:
        """返回各肌肉特征的当前值（**相对脸部尺寸的比例**，距离无关）。

        以双眼外角距离 face_ref 为尺度基准：
          长度为量纲的除以 face_ref；面积为量纲的除以 face_ref²。
        因此与摄像头距离无关：无论离镜头远近，同一咀嚼动作
        得到的特征值基本一致。

        jaw_v        : 下巴相对双眼中线的垂直偏移 / face_ref
        jaw_width    : 左右下颌角水平距离 / face_ref
        masseter_area: 双侧咬肌 ROI 面积之和 / face_ref²
        temporalis_d : 双侧颞肌 ROI 线段长度之和 / face_ref
        lip_openness : 13-14 垂直距离 / face_ref
        """
        out = {
            "jaw_v": None,
            "jaw_width": None,
            "masseter_area": None,
            "temporalis_d": None,
            "lip_openness": None,
            "face_center": None,
            "face_ref": None,
        }
        if not lms:
            return out

        eyes = self._safe_pts(lms, [self.EYE_L, self.EYE_R])
        if len(eyes) < 2:
            return out
        face_ref = float(np.hypot(eyes[1][0] - eyes[0][0], eyes[1][1] - eyes[0][1]))
        if face_ref < self.min_face_ref:   # 脸太小/检测退化，放弃本帧
            return out

        out["face_ref"] = face_ref
        eye_mid_x = float((eyes[0][0] + eyes[1][0]) / 2.0)
        eye_mid_y = float((eyes[0][1] + eyes[1][1]) / 2.0)
        out["face_center"] = (eye_mid_x, eye_mid_y)

        chin = self._safe_pts(lms, [self.CHIN])
        jaw = self._safe_pts(lms, [self.JAW_L, self.JAW_R])
        mass = self._safe_pts(lms, self.MASSETER_L + self.MASSETER_R)
        temp = self._safe_pts(lms, self.TEMPORALIS_L + self.TEMPORALIS_R)
        lip = self._safe_pts(lms, [self.LIP_TOP, self.LIP_BOTTOM])

        eye_mid_y = (eyes[0][1] + eyes[1][1]) / 2.0
        if len(chin) == 1:
            out["jaw_v"] = float((chin[0][1] - eye_mid_y) / face_ref)
        if len(jaw) == 2:
            out["jaw_width"] = float(abs(jaw[1][0] - jaw[0][0]) / face_ref)
        if len(mass) >= 6:
            area_l = self._polygon_area(self._safe_pts(lms, self.MASSETER_L))
            area_r = self._polygon_area(self._safe_pts(lms, self.MASSETER_R))
            out["masseter_area"] = float((area_l + area_r) / (face_ref * face_ref))
        if len(temp) >= 4:
            dl = self._dist(lms, self.TEMPORALIS_L)
            dr = self._dist(lms, self.TEMPORALIS_R)
            out["temporalis_d"] = float((dl + dr) / face_ref)
        if len(lip) == 2:
            out["lip_openness"] = float(abs(lip[1][1] - lip[0][1]) / face_ref)
        return out

    @staticmethod
    def _dist(lms, ids):
        if len(ids) >= 2 and 0 <= ids[0] < len(lms) and 0 <= ids[1] < len(lms):
            a, b = lms[ids[0]], lms[ids[1]]
            return float(np.hypot(a[0] - b[0], a[1] - b[1]))
        return 0.0


# =====================================================================
#  3) 训练与检测模型
# =====================================================================

FEATURE_KEYS = ["jaw_v", "jaw_width", "masseter_area", "temporalis_d", "lip_openness"]
# 投票权重：下巴为主，咬肌次之，下颌角与颞肌辅助
VOTE_WEIGHTS = {
    "jaw_v": 0.35,
    "masseter_area": 0.30,
    "jaw_width": 0.20,
    "temporalis_d": 0.15,
}


class ChewingModel:
    """咀嚼判定模型：逻辑回归分类器，由「点击标定」训练。

    训练：点按 C 前后窗口的帧 = 咀嚼正样本；未点按的帧 = 非咀嚼负样本。
          用这两类样本训练逻辑回归，学出各肌肉特征对咀嚼的权重。
    检测：逐帧计算「咀嚼概率」；概率≥0.5 且连续 enough_frames 帧
          才计一次咀嚼（够帧数固定 5 帧）。
    """

    def __init__(self):
        self.version = "2.0"
        # 机器学习参数（逻辑回归：标准化均值/标准差 + 权重 w + 偏置 b）
        self.ml = None          # {"means": [...], "stds": [...], "w": [...], "b": x}
        # 各特征「活动幅度阈值」：仅供旧模型回退使用（无 ml 参数时）
        self.thresholds = {k: 0.01 for k in FEATURE_KEYS}
        # 投票门槛：仅供旧模型回退使用
        self.vote_gate = 0.55
        # 检测要求「连续满足咀嚼的帧数」：固定值（默认 5 帧）
        self.enough_frames = 5
        # 两次咀嚼最小间隔（秒）
        self.cooldown = 0.45
        # 标定统计信息（仅供报告）
        self.stats = {}
        self.trained_at = None

    @property
    def is_ml(self) -> bool:
        """是否已训练出机器学习分类器"""
        return bool(self.ml)

    # ---- 序列化（ml 参数 + 旧字段兼容）----
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "ml": self.ml,
            "thresholds": self.thresholds,
            "lip_max_open": None,          # 旧占位
            "vote_gate": self.vote_gate,
            "pattern_threshold": self.enough_frames,  # 旧字段名兼容
            "enough_frames": self.enough_frames,
            "cooldown": self.cooldown,
            "stats": self.stats,
            "trained_at": self.trained_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChewingModel":
        m = cls()
        m.version = d.get("version", m.version)
        m.ml = d.get("ml")
        m.thresholds = {k: float(d.get("thresholds", {}).get(k, m.thresholds[k]))
                        for k in FEATURE_KEYS}
        m.vote_gate = float(d.get("vote_gate", m.vote_gate))
        m.enough_frames = int(d.get("enough_frames",
                                    d.get("pattern_threshold", m.enough_frames)))
        m.cooldown = float(d.get("cooldown", m.cooldown))
        m.stats = d.get("stats", {})
        m.trained_at = d.get("trained_at")
        return m

    # ---- 机器学习预测（纯 numpy 逻辑回归） ----
    def ml_prob(self, amps: dict) -> float:
        """将某帧的肌肉活度特征向量输入逻辑回归，输出咬合概率 0~1"""
        if not self.is_ml:
            raise RuntimeError("模型尚未训练 ML 参数")
        means = np.asarray(self.ml["means"], dtype=float)
        stds = np.asarray(self.ml["stds"], dtype=float) + 1e-8
        w = np.asarray(self.ml["w"], dtype=float)
        b = float(self.ml["b"])
        x = np.array([amps.get(k, 0.0) if amps.get(k) is not None else 0.0
                      for k in FEATURE_KEYS], dtype=float)
        z = (x - means) / stds
        z = np.clip(z, -5.0, 5.0)          # 数值稳定
        logit = float(np.dot(z, w) + b)
        return 1.0 / (1.0 + np.exp(-np.clip(logit, -30.0, 30.0)))

    def save(self, filepath: str = "chewing_model.json") -> bool:
        try:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            p_ok(f"模型已保存: {filepath}")
            return True
        except Exception as e:
            p_err(f"模型保存失败: {e}")
            return False

    @classmethod
    def load(cls, filepath: str = "chewing_model.json") -> Optional["ChewingModel"]:
        try:
            if not Path(filepath).exists():
                return None
            with open(filepath, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception as e:
            p_warn(f"模型加载失败: {e}")
            return None


class ChewingTracker:
    """滚动活度计算 + 加权投票 + 咀嚼计数（检测阶段）。"""

    def __init__(self, model: ChewingModel, window_frames: int = 8,
                 motion_shift: float = 0.10, suppress_sec: float = 0.5):
        self.model = model
        self.window_frames = window_frames          # 活度窗口（约 0.27s @30fps）
        self.history: deque = deque(maxlen=window_frames)
        self.pattern_count = 0
        self.last_chew_time = 0.0
        self.chew_count = 0
        self.chew_times: deque = deque(maxlen=500)
        self.last_frame_results = {}

        # 人脸大幅度运动抑制配置
        self.motion_shift = motion_shift   # 每帧归一化位移阈值（脸宽的倍数，默认 0.18=18%脸宽/帧）
        self.suppress_sec = suppress_sec   # 发生大幅移动后抑制计数的冷却时间（秒）
        self.last_suppress_until = 0.0     # 抑制截止时间
        self.last_center = None
        self.last_time = None
        self.is_moving = False

        # 咀嚼概率门槛（可在 models/eating_thresholds.json 里调）
        try:
            from .eating_config import load as _load_eating
            self.prob_threshold = float(_load_eating().get("chew_prob_threshold", 0.5))
        except Exception:
            self.prob_threshold = 0.5

    def check_head_motion(self, feats: dict, t: float) -> bool:
        """检查人脸是否处于大幅度位移状态。

        判据：双眼中点本帧相对上一帧的位移，除以脸部参考宽度（双眼距离）
        得到"归一化位移"（单位=脸宽）。只要单帧位移超过 motion_shift
        （默认 0.18，即一帧内脸移动了 脸宽×18% 以上，转头/俯仰/远离
        都远超这个量级），就判定为大幅移动，并进入持续 suppress_sec 秒
        的抑制期（抑制期内 all 帧都算 moving）。
        归一化后与摄像头距离无关：离镜头近/远，同样的身体动作位移比例一致。
        """
        center = feats.get("face_center")
        face_ref = feats.get("face_ref")
        if center is not None and face_ref is not None and face_ref > 0:
            if self.last_center is not None and self.last_time is not None:
                dx = center[0] - self.last_center[0]
                dy = center[1] - self.last_center[1]
                dist = float(np.hypot(dx, dy))
                shift = dist / face_ref              # 归一化位移（单位：脸宽）
                if shift > self.motion_shift:
                    self.last_suppress_until = t + self.suppress_sec
            self.last_center = center
            self.last_time = t

        is_moving_now = t < self.last_suppress_until
        self.is_moving = is_moving_now
        return is_moving_now

    def update(self, feats: dict, t: float) -> tuple:
        """输入单帧特征，返回 (chew_fired, prob, votes)。
        判定规则：
          ML 模式 —— 逻辑回归输出咀嚼概率 ≥ 0.5 且连续 enough_frames 帧
          才计一次咀嚼（帧数固定 5）。
          旧模式 —— 加权投票得分 ≥ vote_gate 连续 enough_frames 帧。
          抑制逻辑 —— 若检测到头部/人脸大幅度移动，清空连续计数并阻断触发。
        """
        if feats.get("jaw_v") is None:
            return False, 0.0, {}
        self.history.append(feats)

        # 检查是否大幅移动
        moving = self.check_head_motion(feats, t)
        if moving:
            self.pattern_count = 0  # 重置连续满足帧数

        if len(self.history) < max(3, self.window_frames // 2):
            return False, 0.0, {}

        # 逐特征计算窗口内活动幅度（max-min）→ 特征向量
        amps = {}
        frames = list(self.history)
        for k in FEATURE_KEYS:
            vals = [f[k] for f in frames if f.get(k) is not None]
            if len(vals) >= 3:
                amps[k] = float(np.ptp(vals))
            else:
                amps[k] = 0.0

        # 判定值：ML=概率，旧=加权得分
        if self.model.is_ml:
            prob = self.model.ml_prob(amps)
            hit = prob >= self.prob_threshold
            votes = {k: amps.get(k, 0.0) > self.model.thresholds.get(k, 0.01)
                     for k in VOTE_WEIGHTS}   # 仅作 HUD 显示
            score = prob
        else:
            score = 0.0
            wsum = 0.0
            for k, w in VOTE_WEIGHTS.items():
                if amps.get(k, 0.0) > self.model.thresholds.get(k, 0.01):
                    score += w
                wsum += w
            score = score / wsum if wsum > 0 else 0.0
            hit = score >= self.model.vote_gate
            votes = {k: amps.get(k, 0.0) > self.model.thresholds.get(k, 0.01)
                     for k in VOTE_WEIGHTS}
            prob = score

        # 冷却
        if t - self.last_chew_time < self.model.cooldown:
            self.last_frame_results = {"prob": prob, "votes": votes}
            return False, prob, votes

        # 连续满足判定：满足且无人脸大幅度晃动时才计数
        if hit and not moving:
            self.pattern_count += 1
        else:
            self.pattern_count = 0

        fired = False
        need = max(2, int(getattr(self.model, "enough_frames", 3)))
        if self.pattern_count >= need and not moving:
            fired = True
            self.chew_count += 1
            self.chew_times.append(t)
            self.last_chew_time = t
            self.pattern_count = 0

        self.last_frame_results = {"prob": prob, "votes": votes}
        return fired, prob, votes

    def chews_per_min(self, t: float) -> int:
        while self.chew_times and t - self.chew_times[0] > 60:
            self.chew_times.popleft()
        return len(self.chew_times)


# =====================================================================
#  4) 训练器：以按键前后连续帧为正样本学习阈值
# =====================================================================

class ChewingTrainer:
    """收集按键标注样本 → 统计基线噪声与咀嚼活动 → 生成 ChewingModel。

    原理：
      - 全局所有帧的滚动活度 P50 ≈ 静止噪声基线
      - 按键窗口（±WINDOW_SEC）内活度 P25 ≈ 咀嚼时的典型活动幅度
      - 阈值 = max(咀嚼活度×0.55, 噪声×1.8)  兼顾灵敏度与防误报
      - 唇开度上限 = 按键窗口唇开度 P95×1.35（超过视为说话/哈欠）
    """

    WINDOW_SEC = 0.5          # 按键前后采样窗口（连续视频帧数 ≈ 30fps×0.5s×2）
    ROLLING = 8               # 活度滚动窗口帧数

    SESSION_PATH = _DEFAULT_SESSION_PATH

    def __init__(self, model_dir: str = _MODELS_DIR):
        self.backend = FaceMeshBackend(model_dir)
        self.features = ChewingMuscleFeatures()
        self.samples: deque = deque(maxlen=3600 * 5)  # 全流程帧特征（含时间戳）
        self.chew_marks: List[float] = []          # 按键时刻列表
        self.frame_no = 0
        self._session_t0 = None    # 会话时间基点（绝对值），用于跨会话时间轴连续

    def _now_rel(self) -> float:
        """相对会话基点的当前时间。无基点（首次）则直接用绝对时间。"""
        if self._session_t0 is None:
            self._session_t0 = time.time()
        return time.time() - self._session_t0

    def record_frame(self, feats: dict, t: Optional[float] = None):
        feats["_t"] = t if t is not None else self._now_rel()
        feats["_n"] = self.frame_no
        self.frame_no += 1
        self.samples.append(feats)

    def mark_chew(self):
        self.chew_marks.append(self._now_rel())
        p_ok(f"咀嚼标注 #{len(self.chew_marks)}")

    # ---- 会话存取：Q 中断后下次可继续训练 ----
    def save_session(self, filepath: str = SESSION_PATH) -> bool:
        """把已采集的帧样本与点击时刻存盘（时间轴相对会话基点）"""
        try:
            t0 = self._session_t0 if self._session_t0 is not None else time.time()
            frames = []
            for f in self.samples:
                rec = {k: f[k] for k in FEATURE_KEYS}
                rec["jaw_v"] = f.get("jaw_v")
                rec["jaw_width"] = f.get("jaw_width")
                rec["masseter_area"] = f.get("masseter_area")
                rec["temporalis_d"] = f.get("temporalis_d")
                rec["lip_openness"] = f.get("lip_openness")
                rec["_t"] = f["_t"]
                rec["_n"] = f.get("_n", 0)
                frames.append(rec)
            data = {
                "t0": t0,
                "frame_no": self.frame_no,
                "frames": frames,
                "chew_marks": self.chew_marks,
            }
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            p_ok(f"训练进度已保存: {filepath}（{len(frames)} 帧 / {len(self.chew_marks)} 次点击）")
            return True
        except Exception as e:
            p_err(f"进度保存失败: {e}")
            return False

    def load_session(self, filepath: str = SESSION_PATH) -> bool:
        """恢复上次中断的训练进度；继续采集时时间轴自动顺延"""
        try:
            if not Path(filepath).exists():
                return False
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            frames = data.get("frames", [])
            chew = data.get("chew_marks", [])
            if not frames:
                return False
            self.samples = deque(frames, maxlen=self.samples.maxlen)
            self.chew_marks = list(chew)
            self.frame_no = int(data.get("frame_no", len(frames)))
            self._session_t0 = float(data.get("t0", time.time()))
            p_ok(f"已恢复上次训练进度: {len(frames)} 帧 / {len(chew)} 次点击，"
                 f"可继续点按补充样本")
            return True
        except Exception as e:
            p_err(f"进度恢复失败: {e}")
            return False

    def clear_session(self, filepath: str = SESSION_PATH):
        try:
            if Path(filepath).exists():
                Path(filepath).unlink()
        except Exception:
            pass

    # ---- 训练主逻辑 ----
    def _positive_frames(self, frames):
        """按键窗口内的帧（正样本）集合（按索引归集）"""
        pos_idx = set()
        for mark in self.chew_marks:
            for i, f in enumerate(frames):
                if abs(f["_t"] - mark) <= self.WINDOW_SEC:
                    pos_idx.add(i)
        return pos_idx

    def _frame_amps(self, frames):
        """逐帧滚动活度（8帧窗口内各特征 max-min）"""
        amps = {k: [0.0] * len(frames) for k in FEATURE_KEYS}
        win = deque(maxlen=self.ROLLING)
        for i, f in enumerate(frames):
            win.append(f)
            if len(win) < 3:
                continue
            for k in FEATURE_KEYS:
                vals = [x[k] for x in win if x.get(k) is not None]
                if len(vals) >= 3:
                    amps[k][i] = float(np.ptp(vals))
        return amps

    def _score_of(self, amps, i, thresholds):
        """按学习到的阈值计算第 i 帧的加权投票得分"""
        score = 0.0
        wsum = 0.0
        for k, w in VOTE_WEIGHTS.items():
            if amps[k][i] > thresholds.get(k, 0.01):
                score += w
            wsum += w
        return score / wsum if wsum > 0 else 0.0

    def _window_peak_amps(self, amps, frames):
        """每个按键窗口内的活度峰值（咀嚼的典型幅度）。

        正样本窗口包含咀嚼+周围静止帧，直接统计会被静止帧稀释，
        因此取每个窗内各特征的最大活度作为该次咀嚼的幅度样本。
        """
        peaks = {k: [] for k in FEATURE_KEYS}
        for mark in self.chew_marks:
            win_peak = {k: 0.0 for k in FEATURE_KEYS}
            for i, f in enumerate(frames):
                if abs(f["_t"] - mark) <= self.WINDOW_SEC:
                    for k in FEATURE_KEYS:
                        if amps[k][i] > win_peak[k]:
                            win_peak[k] = amps[k][i]
            for k in FEATURE_KEYS:
                peaks[k].append(win_peak[k])
        return peaks

    @staticmethod
    def _train_logistic(X: np.ndarray, y: np.ndarray, epochs: int = 400,
                        lr: float = 0.2, l2: float = 1e-3) -> dict:
        """纯 numpy 逻辑回归训练（特征标准化 + 类别权重 + L2 正则）。

        X: (n, d) 特征矩阵（各帧肌肉活度）
        y: (n,)   标签（1=点击正样本咀嚼，0=未点击非咀嚼）
        返回 {"means","stds","w","b"} —— 检测时用 sigmoid(w·z + b) 输出概率。
        """
        n, d = X.shape
        if n == 0 or d == 0:
            raise RuntimeError("无有效训练样本")
        n_pos = int(np.sum(y == 1))
        n_neg = int(np.sum(y == 0))
        if n_pos < 3 or n_neg < 3:
            raise RuntimeError(
                f"正/负样本不足（正{n_pos} 负{n_neg}）：请多点击几次 C，并留出未点按的时段")

        # 标准化（检测端需用同一均值/标准差）
        means = X.mean(axis=0)
        stds = X.std(axis=0) + 1e-8
        Z = (X - means) / stds
        Z = np.clip(Z, -6.0, 6.0)

        # 类别权重：平衡正负样本（避免负样本主导）
        weight_pos = 1.0 / n_pos
        weight_neg = 1.0 / n_neg
        wts = np.where(y == 1, weight_pos, weight_neg)

        w = np.zeros(d)
        b = 0.0
        for _ in range(epochs):
            z = Z @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
            err = (p - y) * wts            # 加权残差
            gw = Z.T @ err + l2 * w        # 含 L2 正则梯度
            gb = float(np.sum(err))
            # 简单自适应步长（防震荡）
            gnorm = float(np.sqrt(np.sum(gw * gw) + gb * gb))
            if gnorm > 1e-9:
                step = lr / max(1.0, gnorm)
                w -= step * gw
                b -= step * gb

        return {
            "means": [round(float(v), 6) for v in means],
            # std 保底 1e-4：避免微小方差被 round 成 0 → 推理除零
            "stds": [round(float(max(v, 1e-4)), 6) for v in stds],
            "w": [round(float(v), 6) for v in w],
            "b": round(float(b), 6),
        }

    def train(self) -> ChewingModel:
        """机器学习训练：点击标注 → 逻辑回归分类器。

        正样本 = 每次点按 C 前后 ±WINDOW_SEC 窗口内的帧（你在咀嚼）
        负样本 = 其余所有未点按的帧（停顿/说话/其他 —— 均非咀嚼）
        模型   = 逻辑回归（4~5 维肌肉活度特征 → 咀嚼概率）
        判定   = 概率≥0.5 且连续够帧数（固定 5 帧）
        """
        frames = [f for f in self.samples if f.get("jaw_v") is not None]
        if len(frames) < 30:
            raise RuntimeError(f"有效帧不足（{len(frames)}），请正对摄像头后重新采集")
        if len(self.chew_marks) < 2:
            raise RuntimeError("点击标注不足（至少 2 次 C 键），请连续点按多次")

        specs = self.chew_marks[:]
        specs.sort()
        amps = self._frame_amps(frames)
        pos_idx = self._positive_frames(frames)
        model = ChewingModel()

        # 1) 组装训练集：每一帧的活度向量 + 标签
        X = np.array([[amps[k][i] for k in FEATURE_KEYS]
                      for i in range(len(frames))], dtype=float)
        y = np.array([1.0 if i in pos_idx else 0.0
                      for i in range(len(frames))], dtype=float)

        # 2) 训练逻辑回归
        model.ml = self._train_logistic(X, y)

        # 3) 训练集自评估（报告用）
        means = np.asarray(model.ml["means"])
        stds = np.asarray(model.ml["stds"])
        w = np.asarray(model.ml["w"])
        b = float(model.ml["b"])
        z = (X - means) / stds
        z = np.clip(z, -6.0, 6.0)
        prob = 1.0 / (1.0 + np.exp(-np.clip(z @ w + b, -30.0, 30.0)))
        pred = (prob >= 0.5).astype(int)
        tp = int(np.sum((pred == 1) & (y == 1)))
        fp = int(np.sum((pred == 1) & (y == 0)))
        fn = int(np.sum((pred == 0) & (y == 1)))
        tn = int(np.sum((pred == 0) & (y == 0)))
        n_pos = int(np.sum(y == 1))
        n_neg = int(np.sum(y == 0))
        model.stats["frames"] = len(frames)
        model.stats["pos_frames"] = n_pos
        model.stats["neg_frames"] = n_neg
        model.stats["chew_marks"] = len(self.chew_marks)
        model.stats["train_acc"] = round(float(np.mean(pred == y)), 4)
        model.stats["recall"] = round(float(tp / n_pos), 4) if n_pos else 0.0
        model.stats["precision"] = round(float(tp / (tp + fp)), 4) if (tp + fp) else 0.0
        model.stats["false_alarm"] = round(float(fp / max(n_neg, 1)), 4)
        if len(specs) >= 2:
            dt = [specs[i + 1] - specs[i] for i in range(len(specs) - 1)
                  if 0.05 < specs[i + 1] - specs[i] < 2.0]
            if dt:
                model.stats["click_interval_median_s"] = round(float(np.median(dt)), 4)
        model.enough_frames = 5
        model.stats["duration_frames"] = model.enough_frames
        model.trained_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return model

    def report(self, model: ChewingModel):
        print("\n=========== 训练结果报告（机器学习·逻辑回归） ===========")
        print(f"有效帧: {model.stats.get('frames')} | "
              f"正样本(点击): {model.stats.get('pos_frames')} 帧 | "
              f"负样本(未点击): {model.stats.get('neg_frames')} 帧 | "
              f"咀嚼标注: {model.stats.get('chew_marks')} 次")
        if model.is_ml:
            print("模型: 逻辑回归 (纯 numpy)")
            feats_desc = {
                "jaw_v": "下巴", "jaw_width": "颌角", "masseter_area": "咬肌",
                "temporalis_d": "颞肌", "lip_openness": "唇开度"}
            print(f"{'特征':<10}{'学得权重':>12}   (权重越大越关键)")
            for k, wi in zip(FEATURE_KEYS, model.ml["w"]):
                print(f"{feats_desc.get(k, k):<10}{wi:>12.4f}")
            print(f"训练集: 准确率 {model.stats.get('train_acc')} | "
                  f"召回 {model.stats.get('recall')} | "
                  f"精确率 {model.stats.get('precision')} | "
                  f"误报率 {model.stats.get('false_alarm')}")
            prec = model.stats.get("precision")
            if prec is not None and float(prec) < 0.7:
                p_warn(f"精确率偏低（{prec}）——判定成咀嚼的帧里约 "
                       f"{round((1 - float(prec)) * 100)}% 其实是误报，会虚高计数。")
                print("        改进办法: ①标定时「一直在嚼就一直点 C」，别只点一两下；")
                print("                  ②不该咀嚼的时段保持不动、不说话（负样本要干净）；")
                print("                  ③或把 models/eating_thresholds.json 里的 "
                      "chew_prob_threshold 提到 0.65~0.7 压制误报。")
        else:
            print("警告: 模型不含 ML 参数，将回退旧阈值投票模式")
        print(f"检测要求连续满足帧数: {model.enough_frames} 帧（固定值）")
        print(f"冷却: {model.cooldown}s")


# =====================================================================
#  5) 主程序：菜单 / 标定会话 / 检测会话
# =====================================================================

def draw_hud(frame, score, votes, chew_count, cpm, feats, is_moving: bool = False):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (10, 10), (w - 10, 140), (0, 0, 0), -1)
    y = 34
    cv2.putText(frame, f"Chews total: {chew_count}  |  {cpm} /min",
                (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    names = {"jaw_v": "jaw(175)", "jaw_width": "jawW(234/454)",
             "masseter_area": "masseter", "temporalis_d": "temporalis"}
    txt = " ".join(f"{names[k]}={'Y' if votes.get(k) else 'n'}" for k in VOTE_WEIGHTS)
    cv2.putText(frame, txt, (20, y + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (255, 255, 255), 1)

    if is_moving:
        color = (0, 0, 255)
        state = "MOVING HEAD (PAUSED)"
    else:
        color = (0, 255, 0) if score >= 0.5 else (200, 200, 200)
        state = f"CHEWING {score:.2f}" if score >= 0.5 else f"idle {score:.2f}"

    cv2.putText(frame, state, (20, y + 52), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
    cv2.putText(frame, "Keys: [C]=mark chew  [S]=save  [Q]=quit",
                (20, y + 78), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return frame


def run_calibration(model_path: str = _DEFAULT_MODEL_PATH):
    """交互标定：按 C 记录按键前后帧，按 S 训练预览，Enter 保存。"""
    print("============================================================")
    print("  交互标定模式（多肌肉共同判定咀嚼）")
    print("  1. 正对摄像头，光线充足，保持与平时吃饭一致的距离")
    print("  2. 咀嚼时**连续点按 [C]**（嚼几口按几次），建议共点按 10~30 次")
    print("     点按前后约 0.5 秒的帧 = 咀嚼正样本（机器学习用）")
    print("  3. 未点按的帧（停顿/说话/其他动作）= 非咀嚼负样本")
    print("     建议：标定中途停嘴几秒不按，让机器学习两种状态")
    print("  4. 训练逻辑回归输出咀嚼概率（≥0.5 且连续 5 帧判定为一次）")
    print("  5. 按 [S] 训练预览  |  按 [Enter] 保存  |  按 [Q] 退出")
    print("============================================================")
    input("准备好后按 Enter 启动摄像头...")

    trainer = ChewingTrainer()
    # 上次中断留下的训练进度 → 询问是否继续（Q 退出会自动保存进度）
    if Path(ChewingTrainer.SESSION_PATH).exists():
        ans = input("检测到上次中断的训练进度，是否继续训练？(y/N): ").strip().lower()
        if ans in ("y", "yes"):
            trainer.load_session()
        else:
            trainer.clear_session()
    if not trainer.backend.available:
        p_err("FaceMesh 不可用，无法标定")
        return
    p_ok(f"FaceMesh 后端: {trainer.backend.backend}")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        p_warn("无法打开摄像头，转入模拟演示")
        run_demo()
        return

    trained_model = None
    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            display = cv2.flip(frame, 1)
            h, w = display.shape[:2]
            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            lms = trainer.backend.detect(rgb)
            feats = trainer.features.extract(lms)
            trainer.record_frame(feats)

            # HUD（标定模式显示实时特征值）
            cv2.rectangle(display, (10, 10), (w - 10, 90), (0, 0, 0), -1)
            vals = (f"jawY:{feats.get('jaw_v') or 0:.3f} "
                    f"jawW:{feats.get('jaw_width') or 0:.3f} "
                    f"mass:{feats.get('masseter_area') or 0:.4f} "
                    f"temp:{feats.get('temporalis_d') or 0:.3f} "
                    f"lip:{feats.get('lip_openness') or 0:.3f}")
            cv2.putText(display, f"Marks: {len(trainer.chew_marks)}", (20, 34),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(display, f"Feat: {vals}", (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.putText(display, "[C]=mark [S]=train [Enter]=save [Q]=quit",
                        (20, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imshow("Chewing Calibration", display)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('c'), ord('C')):
                trainer.mark_chew()
            elif key in (ord('s'), ord('S')):
                try:
                    trained_model = trainer.train()
                    trainer.report(trained_model)
                except RuntimeError as e:
                    p_err(str(e))
            elif key in (ord('\r'), ord('\n')):
                if trained_model is None:
                    try:
                        trained_model = trainer.train()
                        trainer.report(trained_model)
                    except RuntimeError as e:
                        p_err(f"样本不足: {e}")
                        continue
                trained_model.save(model_path)
                p_ok("已保存。按 Q 退出标定，进入模式 2 实时检测验证")
            elif key in (ord('q'), ord('Q')):
                break
    except Exception:
        raise
    finally:
        cap.release()
        cv2.destroyAllWindows()
        if trained_model is not None:
            trainer.clear_session()   # 训练并保存成功 → 清掉旧进度
        else:
            # 中断（Q 退出/关窗/还没保存模型）→ 保留进度，下次可继续
            if len(trainer.samples) > 0:
                trainer.save_session()


def run_detection(model_path: str = _DEFAULT_MODEL_PATH):
    """实时咀嚼检测：加载(或默认)模型，多肌肉投票实时判定。"""
    model = ChewingModel.load(model_path)
    if model is None:
        p_warn(f"未找到 {model_path}，使用默认参数（建议先跑模式 1 标定）")
        model = ChewingModel()

    backend = FaceMeshBackend()
    if not backend.available:
        p_err("FaceMesh 不可用")
        return
    p_ok(f"FaceMesh 后端: {backend.backend}")

    feats_ext = ChewingMuscleFeatures()
    tracker = ChewingTracker(model)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        p_warn("无法打开摄像头，转入模拟演示")
        run_demo()
        return

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            display = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            lms = backend.detect(rgb)
            feats = feats_ext.extract(lms)
            t = time.time()
            fired, score, votes = tracker.update(feats, t)
            cpm = tracker.chews_per_min(t)
            display = draw_hud(display, score, votes,
                               tracker.chew_count, cpm, feats,
                               is_moving=tracker.is_moving)
            cv2.imshow("Chewing Detection", display)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), ord('Q')):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


def verify_motion_suppression():
    """离线自测：人脸大幅度移动时**不**进行咀嚼计数（无需摄像头）。

    用合成的"咀嚼特征帧"喂给 ChewingTracker：
      段1 静止    + 咀嚼特征  → 应有计数
      段2 大幅移动 + 咀嚼特征  → 必须 0 计数（本次改进的核心）
      段3 恢复静止 + 咀嚼特征  → 重新有计数
    """
    model = ChewingModel()                # 默认投票模式：阈值 0.01 / vote_gate 0.55
    tracker = ChewingTracker(model, motion_shift=0.18, suppress_sec=0.5)
    dt = 1.0 / 30.0
    t = 0.0
    fires = {"still": 0, "moving": 0}

    def chew_frame(phase):
        # 与提取特征同构：四路咀嚼特征都振荡到超过阈值 0.01（保证命中投票）
        return {
            "jaw_v": 0.10 + 0.020 * np.sin(phase),
            "jaw_width": 0.55 + 0.010 * np.sin(phase + 0.6),
            "masseter_area": 0.020 + 0.008 * np.sin(phase + 1.2),
            "temporalis_d": 0.30 + 0.008 * np.sin(phase + 0.9),
            "lip_openness": 0.03,
            "face_center": (0.50, 0.45),
            "face_ref": 0.25,
        }

    def idle_frame():
        f = chew_frame(0.0)
        f["lip_openness"] = 0.02
        return f

    # 段1：静止 + 咀嚼特征（40 帧）→ 应计数
    for i in range(40):
        fired, _, _ = tracker.update(chew_frame(2 * np.pi * (i % 10) / 10.0), t)
        if fired:
            fires["still"] += 1
        t += dt
    # 段2：每帧人脸中心大幅跳变 + 同样咀嚼特征（40 帧）→ 必须不计数
    moving_pts = [(0.65, 0.50), (0.40, 0.38)]   # 单帧位移 ≈0.12/0.25=0.48 脸宽 >> 0.18
    for i in range(40):
        f = chew_frame(2 * np.pi * (i % 10) / 10.0)
        f["face_center"] = moving_pts[i % 2]
        fired, _, _ = tracker.update(f, t)
        if fired:
            fires["moving"] += 1
        t += dt
    # 段间：静止歇 20 帧，让抑制期过去、窗口清空
    for _ in range(20):
        tracker.update(idle_frame(), t)
        t += dt
    # 段3：恢复静止 + 咀嚼特征（40 帧）→ 重新计数
    for i in range(40):
        fired, _, _ = tracker.update(chew_frame(2 * np.pi * (i % 10) / 10.0), t)
        if fired:
            fires["still"] += 1
        t += dt

    ok = fires["still"] >= 2 and fires["moving"] == 0
    p_info(f"头部移动抑制自测: 静止段计数 {fires['still']} 次 | 大幅移动段计数 {fires['moving']} 次")
    if ok:
        p_ok("人脸大幅度移动时不计数：抑制逻辑生效")
    else:
        p_warn(f"抑制逻辑异常: {fires}")
    return ok


def run_demo():
    """无摄像头模拟演示：合成 478 点数据跑通 训练→检测 全流程。"""
    print("\n===== 模拟演示（合成关键点数据）=====")
    rng = np.random.default_rng(42)
    feats_ext = ChewingMuscleFeatures()
    trainer = ChewingTrainer.__new__(ChewingTrainer)
    trainer.backend = None
    trainer.features = feats_ext
    trainer.samples = deque(maxlen=3600)
    trainer.chew_marks = []
    trainer.frame_no = 0

    # 合成运动：每 80 帧一个单元 = 咀嚼(0-14) + 静止(14-44) + 说话(44-60) + 静止(60-80)
    # 点击窗口 ±0.5s=15帧：m=3,7,11 覆盖咀嚼段；未点按的静止/说话为负样本
    unit = 80

    def synth_frame(i, in_chew, talking, scale=1.0):
        base = []
        base_y = 0.60
        for p in range(478):
            base.append((0.5 + rng.normal(0, 0.001), base_y + rng.normal(0, 0.001)))
        m = i % unit
        phase = 2 * np.pi * (m % 10) / 10.0

        # 双眼外角 33/263：尺度基准（固定，咀嚼不动）
        eye_ref = 0.28 * scale          # 双眼距离（按 scale 缩放模拟远近）
        base[33] = (0.5 - eye_ref / 2, 0.47)
        base[263] = (0.5 + eye_ref / 2, 0.47)

        # 主信号：下巴最低点(175) 咀嚼时上下往复 ±0.025（天然相对脸的比例）
        chin_y = base_y + (0.025 * np.sin(phase) if in_chew else 0.0)
        base[175] = (0.5, round(float(chin_y), 4))

        # 协同1：下颌角(234/454) 咀嚼时左右不对称微摆（宽度脉动 ±0.006）
        sway = (0.006 * np.sin(phase + 0.6)) if in_chew else 0.0
        base[234] = (0.42 - sway, 0.72)
        base[454] = (0.58 + sway, 0.72)

        # 协同2：咬肌 ROI 4点×2侧，咀嚼时向外膨胀（面积明显变化）
        puff = 0.012 if in_chew else 0.0
        base[148] = (0.40 - puff, 0.66); base[176] = (0.44 + puff, 0.70)
        base[192] = (0.41 + puff, 0.74); base[214] = (0.38 - puff, 0.70)
        base[374] = (0.60 + puff, 0.66); base[400] = (0.56 - puff, 0.70)
        base[416] = (0.59 - puff, 0.74); base[436] = (0.62 + puff, 0.70)

        # 协同3：颞肌 ROI 2点×2侧，用力咬合微凸（距离变化）
        temp = (0.006 * np.sin(phase + 1.2)) if in_chew else 0.0
        base[109] = (0.36 - temp, 0.40); base[127] = (0.37 + temp, 0.44)
        base[338] = (0.64 + temp, 0.40); base[356] = (0.63 - temp, 0.44)

        # 唇开度 13/14（仅作特征保留，不再用于过滤）
        lip = 0.06 if in_chew else (0.24 if talking else 0.02)
        base[13] = (0.5, 0.62 - lip / 2)
        base[14] = (0.5, 0.62 + lip / 2)
        return base

    total = unit * 6  # 480 帧 ≈ 6 个单元（6 次咀嚼）
    for i in range(total):
        m = i % unit
        in_chew = m < 14
        talking = 44 <= m < 60
        lms = synth_frame(i, in_chew, talking)
        feats = feats_ext.extract(lms)
        trainer.record_frame(feats, t=float(i) / 30.0)
        # 模拟用户「咀嚼时连续点按 C」：咀嚼段内连按 3 次（m=3,7,11）
        if in_chew and m in (3, 7, 11):
            trainer.chew_marks.append(feats["_t"])

    model = trainer.train()
    trainer.report(model)

    # 检测验证
    tracker = ChewingTracker(model)
    chew_fired_n = 0
    last_fired_i = -99
    cheat_count = 0  # 说话/静止段被误判为咀嚼的次数
    for i in range(total):
        m = i % unit
        in_chew = m < 14
        talking = 44 <= m < 60
        lms = synth_frame(i, in_chew, talking)
        feats = feats_ext.extract(lms)
        fired, score, votes = tracker.update(feats, float(i) / 30.0)
        if fired and i - last_fired_i > 8:
            chew_fired_n += 1
            last_fired_i = i
            # 验证口径与训练一致：是否落在任一点击窗口（±0.5s）内
            t_now = float(i) / 30.0
            in_any_window = any(abs(mk - t_now) <= 0.5 for mk in trainer.chew_marks)
            if not in_any_window:
                cheat_count += 1
    print(f"\n检测验证: 触发咀嚼 {chew_fired_n} 次 (期望≈6=单元数) | "
          f"非点击段误判 {cheat_count} 次")
    print(f"连续帧要求: {model.enough_frames} 帧（固定值）")
    ok = 4 <= chew_fired_n <= 10 and cheat_count == 0
    p_ok("模拟演示通过：连续帧判定能检出咀嚼且无误判" if ok
         else "模拟演示数值异常，请检查合成数据/阈值")

    # 本次新增功能的独立验证：人脸大幅移动时不计数
    verify_motion_suppression()
    return model


def main():
    import argparse

    ap = argparse.ArgumentParser(description="咀嚼多肌肉协同检测器（训练/检测）")
    ap.add_argument("--calibrate", action="store_true",
                    help="交互标定并训练专属模型（咀嚼时连续点按 C，S 训练，回车保存）")
    ap.add_argument("--detect", action="store_true", help="用已保存的模型做实时检测")
    ap.add_argument("--demo", action="store_true", help="无摄像头模拟演示")
    args = ap.parse_args()
    if args.calibrate:
        run_calibration()
        return
    if args.detect:
        run_detection()
        return
    if args.demo:
        run_demo()
        return

    print("============================================================")
    print("  咀嚼多肌肉协同检测器  (mediapipe 468点 · 连续点按训练)")
    print("  主信号: 下巴(175) | 协同: 下颌角(234/454)、咬肌ROI、颞肌ROI")
    print("  判定: 连续 5 帧同时满足咀嚼条件才计一次咀嚼")
    print(f"  模型保存位置: {_DEFAULT_MODEL_PATH}")
    print("============================================================")
    while True:
        print("\n请选择模式:")
        print("  1. 交互标定（咀嚼时连续点按 C，训练专属模型）")
        print("  2. 实时咀嚼检测（使用已标定/默认模型）")
        print("  3. 无摄像头模拟演示")
        print("  0. 退出")
        choice = input("选择 (0-3): ").strip()
        if choice == "1":
            run_calibration()
        elif choice == "2":
            run_detection()
        elif choice == "3":
            run_demo()
        elif choice == "0":
            break
        else:
            p_warn("无效选择")


if __name__ == "__main__":
    main()


