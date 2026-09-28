"""摄像头视觉服务 —— 五线程互不阻塞：
  1. 采集线程：按相机原生帧率抓帧（read() 自带相机帧周期阻塞，新帧事件通知编码线程）
  2. 编码线程：事件驱动逐帧发布 —— 960 宽 JPEG 编码 + 320 宽运动分析（~4ms/帧，
     远小于相机 33ms 帧周期，流帧率因此能跟满相机上限）
  3. 手口线程：MediaPipe 送食计数 + 咀嚼速率（默认 1.2s 一次）
  4. 感知线程：PerceptionHub（HSEmotion 情绪 + 肌肉法咀嚼）
  5. GLM-4V 线程：每 10s 结构化分析（人脸/表情/分心物/食物/场景）

内部帧格式统一 BGR（cv2 原生），编码/运动分析零颜色转换；MediaPipe 单独转 RGB。
摄像头来源自动降级：Picamera2 → 本机摄像头(OpenCV) → Mock 模拟画面。
全局单例 vision_service 供 API 层共享（感知模型全进程只加载一份）。
"""
from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .. import config
from . import eating_config
from .hub import PerceptionHub

_VALID_EMOTION = {"happy", "sad", "angry", "surprise", "neutral",
                  "love", "sleepy", "cool", "cry", "wink", "shy"}


def _frame_to_bmp_bytes(frame: np.ndarray) -> bytes:
    """24bit 无压缩 BMP（纯 Python 编码），返回原始字节（JPEG 失败时的兜底）。"""
    h, w = frame.shape[:2]
    row_size = (w * 3 + 3) & ~3
    data_size = row_size * h
    header = b"BM" + struct.pack("<IHHI", 14 + 40 + data_size, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, data_size, 2835, 2835, 0, 0)
    rows = []
    for y in range(h - 1, -1, -1):
        row = np.ascontiguousarray(frame[y])[:, ::-1]
        rows.append(row.tobytes() + b"\x00" * (row_size - w * 3))
    return header + b"".join(rows)


def _frame_to_jpeg_bytes(frame_bgr: np.ndarray, quality: int = 72) -> bytes | None:
    """BGR 帧 → JPEG 字节（cv2 原生格式，零颜色转换开销）。"""
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {}


class BiteTracker:
    """送食计数：指尖进入嘴部范围记一次，离开嘴边之后才允许计下一次。

    旧逻辑是"连续 N 次采样都命中"，但手口采样间隔比一次送食动作还长，
    实际几乎不可能连续命中，于是进食次数长期为 0。现在改成
    「进入一次记一次 + 必须离开过 + 冷却时间」。
    """

    def __init__(self, min_near_sec: float, leave_sec: float,
                 refractory_sec: float, window_sec: float):
        self.min_near_sec = min_near_sec
        self.leave_sec = leave_sec
        self.refractory_sec = refractory_sec
        self.window_sec = window_sec
        self._records: list[float] = []
        self._last_bite = float("-inf")     # 第一次进入即可计，不受冷却限制
        self._was_near = False
        self._near_since = 0.0
        self._far_since = 0.0
        self._left_mouth = True

    def update(self, near: bool, now: float) -> bool:
        """喂入一次"指尖是否贴近嘴"，返回本次是否记为一次送食。"""
        fired = False
        if near:
            if not self._was_near:
                self._near_since = now
            if (self._left_mouth
                    and (now - self._near_since) >= self.min_near_sec
                    and (now - self._last_bite) >= self.refractory_sec):
                self._last_bite = now
                self._left_mouth = False
                fired = True
                self._records.append(now)
            self._far_since = 0.0
        else:
            if not self._far_since:
                self._far_since = now
            if (now - self._far_since) >= self.leave_sec:
                self._left_mouth = True
        self._was_near = near
        self._records = [t for t in self._records if now - t <= self.window_sec]
        return fired

    @property
    def bite_count(self) -> int:
        return len(self._records)

    def per_min(self) -> float:
        return round(len(self._records) / (self.window_sec / 60), 1)


class VisionService:
    """start()/stop() 启停后台线程；snapshot() 取最新帧+全部分析结果。"""

    _EATING = eating_config.load()      # models/eating_thresholds.json，可调不用改代码

    def __init__(self, interval: float | None = None, llm_interval: float | None = None,
                 hand_interval: float | None = None, perception_interval: float | None = None):
        self.interval = interval if interval is not None else 1.0 / config.VISION_FRAME_FPS
        self.llm_interval = llm_interval if llm_interval is not None else config.AI_VISION_INTERVAL
        self.hand_interval = (hand_interval if hand_interval is not None
                              else config.HAND_MOUTH_INTERVAL)
        self.perception_interval = (perception_interval if perception_interval is not None
                                    else config.PERCEPTION_INTERVAL)
        self.show_window = os.environ.get("MINDFUL_SHOW_VISION_WINDOW", "0") == "1"
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._encode_thread: threading.Thread | None = None
        self._llm_thread: threading.Thread | None = None
        self._hand_thread: threading.Thread | None = None
        self._perception_thread: threading.Thread | None = None

        self._latest: dict = {"mode": "idle", "timestamp": None, "frame": None,
                              "frame_format": None, "analysis": {}, "fps": 0.0, "seq": -1}
        self._frame_seq = -1
        # 采集→编码解耦：采集线程按相机原生速率读帧并事件通知；编码循环拿到新帧
        # 立即发布。处理耗时不再叠加到相机帧周期上，流帧率因此能跟满相机上限。
        self._raw_cond = threading.Condition()
        self._raw_frame: np.ndarray | None = None
        self._raw_seq = 0
        self._latest_frame: np.ndarray | None = None
        self._last_frame_jpeg: bytes | None = None
        self._llm_result: dict = {}
        self._hand_result: dict = {"note": "mediapipe 未启用"}
        self._fps = 0.0
        self._picam = None
        self._cap = None
        self._prev_gray = None
        self._hands = None
        self._hand_style = None
        self._face = None
        self._face_style = None
        self._mp = None
        self._mode: str | None = None
        self._mock_since = 0.0        # 进入模拟画面的时刻（用于定期重试真实摄像头）

        self._perception_hub: PerceptionHub | None = None
        self._perception_result: dict = {"available": False, "reason": "未启动"}

        self._robot_emotion_lock = threading.Lock()
        self._robot_emotion = "neutral"

        # 摄像头情绪落库（供健康分析的情绪分布）：变化时记一条，且限频
        self._last_logged_emotion = ""
        self._last_emotion_log_ts = 0.0

        # 语音识别期间暂停感知线程：感知（情绪 ONNX + 人脸/手部关键点）会大量占用
        # CPU 与 GIL，实测会让 ASR 从 2.5 秒劣化到 11.5 秒。
        self._perception_suspended = threading.Event()

    # ---------- 语音优先：识别期间让出算力 ----------
    def suspend_perception(self) -> None:
        """暂停感知线程（供 ASR 期间调用），摄像头画面循环不受影响。"""
        self._perception_suspended.set()

    def resume_perception(self) -> None:
        self._perception_suspended.clear()

    @property
    def perception_suspended(self) -> bool:
        return self._perception_suspended.is_set()

    # ---------- 机器人表情 ----------
    def set_robot_emotion(self, emo: str) -> None:
        if emo not in _VALID_EMOTION:
            return
        with self._robot_emotion_lock:
            self._robot_emotion = emo

    def get_robot_emotion(self) -> str:
        with self._robot_emotion_lock:
            return self._robot_emotion

    # ---------- OpenCV 弹窗 UI（调试用，与网页面板对齐） ----------
    def _draw_overlay_ui(self, frame_bgr: np.ndarray) -> np.ndarray:
        import cv2
        bgr = frame_bgr
        ph, hr = self._perception_result, self._hand_result
        lines = ["==== Real-Perception Model ===="]
        if not ph.get("available"):
            lines.append(f"Model: NOT READY | {ph.get('reason', '')}")
        else:
            em, ch = ph.get("emotion", {}), ph.get("chewing", {})
            lines.append(f"Face:{em.get('face_count', 0)} | Emo:{em.get('emotion_zh', '-')}"
                         f"({round(em.get('confidence', 0.0) * 100)}%) "
                         f"V:{em.get('valence', '-')} A:{em.get('arousal', '-')}")
            chew_state = "●CHEWING" if ch.get("chewing") else ""
            head_warn = "⚠HEAD-MOVE" if ch.get("head_moving") else ""
            lines.append(f"Chew:{ch.get('chews_per_min', 0)}/min total:{ch.get('chew_count', 0)}"
                         f" {chew_state} {head_warn}")
        lines += ["---- MediaPipe Hand-Mouth ----",
                  f"Hands:{hr.get('hands', 0)} NearMouth:{hr.get('hand_near_mouth', False)}",
                  f"Bite:{hr.get('bite_count', 0)}({hr.get('bites_per_min', '-')}/min)",
                  f"ChewMP:{hr.get('chews_per_min', '-')}/min perBite:{hr.get('chews_per_bite', '-')}"]

        h_img, w_img = bgr.shape[:2]
        x0, y0, line_h = 8, 8, 18
        overlay = bgr.copy()
        box_h = int((len(lines) + 1) * line_h)
        cv2.rectangle(overlay, (x0 - 4, y0 - 4), (w_img - 8, y0 + box_h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.65, bgr, 0.35, 0, bgr)
        for idx, txt in enumerate(lines):
            cv2.putText(bgr, txt, (x0, y0 + idx * line_h),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1)
        return bgr

    # ---------- 生命周期 ----------
    def start(self) -> dict:
        if self._running:
            return self.status()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, name="vision-capture", daemon=True)
        self._thread.start()
        self._encode_thread = threading.Thread(target=self._encode_loop, name="vision-encode", daemon=True)
        self._encode_thread.start()
        self._hand_thread = threading.Thread(target=self._hand_loop, name="vision-hands", daemon=True)
        self._hand_thread.start()
        self._llm_thread = threading.Thread(target=self._llm_loop, name="vision-llm", daemon=True)
        self._llm_thread.start()
        self._perception_thread = threading.Thread(target=self._perception_loop,
                                                   name="vision-perception", daemon=True)
        self._perception_thread.start()
        return self.status()

    def stop(self) -> dict:
        self._running = False
        try:
            import cv2
            cv2.destroyAllWindows()
        except Exception:
            pass
        self._release_camera()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "mode": self._latest.get("mode"),
                "timestamp": self._latest.get("timestamp"),
                "interval": self.interval,
                "fps": round(self._fps, 1),
                "llm_interval": self.llm_interval,
                "llm": self._llm_result.get("_ts") is not None,
                "perception": self._perception_available(),
                "robot_emotion": self.get_robot_emotion(),
            }

    def is_running(self) -> bool:
        """摄像头/感知是否在运行（模式切换时设备屏据此显示"已关闭"）。"""
        return self._running

    def _perception_available(self) -> bool:
        """感知就绪判定：模型已加载且最近一次分析没有报错。

        注意：analyze_frame 的返回里没有顶层 available（可用性在 chewing 里），
        直接取 available 会让状态永远显示"未就绪"。
        """
        p = self._perception_result or {}
        if self._perception_hub is None:
            return bool(p.get("available"))
        return not (p.get("reason") or p.get("error"))

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self._latest)
            # 帧数据走 /stream 与 /frame 端点；轮询快照只回分析结果，
            # 避免每次轮询都传输几十~上百 KB 的 base64 帧（前端卡片并不用帧）。
            out.pop("frame", None)
            out.pop("frame_format", None)
            out.pop("seq", None)
            out.setdefault("analysis", {})
            if self._mode == "mock":
                for k, v in self._analysis_mock().items():
                    out["analysis"][k] = v
            hand = dict(self._hand_result)
            out["analysis"]["hand_mouth"] = {
                k: hand[k] for k in ("hands", "hand_near_mouth", "note",
                                     "mouth_dist_ratio") if k in hand}
            if "bites_per_min" in hand:
                out["analysis"]["eating_speed"] = {
                    "bite_count": hand.get("bite_count"),
                    "bites_per_min": hand.get("bites_per_min"),
                    "level": hand.get("level"),
                    "source": "mediapipe-hand-mouth",
                }
            percep_chew = (self._perception_result or {}).get("chewing") or {}
            hand_chews = hand.get("chews_per_min")
            if hand_chews:
                out["analysis"]["chewing"] = {
                    "chews_per_min": hand_chews,
                    "chew_level": hand.get("chew_level"),
                    "chews_per_bite": hand.get("chews_per_bite"),
                    # 累计次数取肌肉法检测器（手口模型没有累计计数器）
                    "chew_count": percep_chew.get("chew_count"),
                    "source": "mediapipe-mouth-openness",
                }
            elif percep_chew:
                # 手不在画面里时手口模型抓不到，回退到肌肉法咀嚼（含累计次数），
                # 避免设备屏的"咀嚼"一直显示占位符
                cpm = float(percep_chew.get("chews_per_min") or 0)
                out["analysis"]["chewing"] = {
                    "chews_per_min": cpm,
                    "chew_level": (("fast" if cpm > 30 else
                                    "slow" if cpm < 15 else "normal") if cpm else None),
                    "chews_per_bite": None,
                    "chew_count": percep_chew.get("chew_count"),
                    "source": "perception-muscle",
                }
            if self._llm_result:
                for k in ("face", "distract", "food", "emotion", "scene"):
                    if self._llm_result.get(k):
                        out["analysis"][k] = self._llm_result[k]
            # 本地 perception 检出人脸时回填"人脸/表情"卡片（GLM-4V 未配置也有数据）
            if "face" not in out["analysis"] or out["analysis"]["face"].get("note"):
                em = (self._perception_result or {}).get("emotion", {})
                if em.get("face_count"):
                    out["analysis"]["face"] = {
                        "face_count": em.get("face_count"),
                        "expression": em.get("emotion_zh") or em.get("emotion"),
                        "source": "local-perception",
                        "confidence": em.get("confidence"),
                    }
            if self._perception_result:
                out["analysis"]["perception"] = self._perception_result
            return out

    def latest_jpeg(self) -> bytes | None:
        """最新一帧 JPEG 原始字节（屏幕线程直接用，不走 base64/analysis 构建）。"""
        return self._last_frame_jpeg

    def latest_stream_frame(self) -> tuple[bytes | None, str | None, int, float]:
        """MJPEG 流轻量取帧：(原始字节, 格式, 帧序号, 时间戳)。

        与 snapshot() 的区别：不加锁构建 analysis、不复制大字典，专供
        /api/vision/stream 每帧调用，避免把帧主循环拖慢。
        """
        with self._lock:
            return (self._latest.get("frame"), self._latest.get("frame_format"),
                    self._latest.get("seq", -1), self._latest.get("timestamp") or 0.0)

    # ---------- 采集线程：按相机原生帧率抓帧 ----------
    def _capture_loop(self) -> None:
        mode = self._probe_mode()
        self._mode = mode
        if mode == "mock":
            self._mock_since = time.time()
        while self._running:
            t0 = time.time()
            try:
                if mode == "picamera":
                    frame = self._capture_picamera()
                elif mode == "webcam":
                    frame = self._capture_webcam()
                else:
                    frame = self._frame_mock()
                self._latest_frame = frame
                with self._raw_cond:
                    self._raw_frame = frame
                    self._raw_seq += 1
                    self._raw_cond.notify_all()
            except Exception:
                if mode != "mock":
                    mode = "mock"
                    self._mode = "mock"
                    self._mock_since = time.time()
                    self._release_camera()
                    continue
                time.sleep(0.05)
                continue
            if mode == "mock":
                # 在模拟画面上时，每 10 秒再探一次真实摄像头：
                # 启动竞态（相机还没就绪）或临时掉线很常见，旧实现在第一次探测失败后
                # 就永久停留在模拟画面，用户会以为"摄像头坏了"。
                if time.time() - self._mock_since > 10.0:
                    self._mock_since = time.time()
                    probed = self._probe_mode()
                    if probed != "mock":
                        mode = probed
                        self._mode = probed
                        continue
                # 相机 read() 自带一个帧周期的阻塞；mock 需要自己限速
                time.sleep(max(0.0, self.interval - (time.time() - t0)))
            else:
                self._mock_since = 0.0

    # ---------- 编码线程：新帧到达即编码发布 ----------
    def _encode_loop(self) -> None:
        """事件驱动逐帧发布：960 宽 JPEG 编码 + 320 宽运动分析共 ~4ms，
        远小于相机 33ms 帧周期，因此流帧率可跟满相机上限（不再叠加处理耗时）。"""
        frame_times: list[float] = []
        last_frame_at = 0.0
        last_raw_seq = -1
        while self._running:
            with self._raw_cond:
                if self._raw_seq == last_raw_seq:
                    self._raw_cond.wait(timeout=0.2)
                    if self._raw_seq == last_raw_seq:
                        continue
                frame = self._raw_frame
                last_raw_seq = self._raw_seq
            if self.show_window:            # 调试弹窗才画 UI 叠层（每帧的 cv2 绘制很费 CPU）
                try:
                    import cv2
                    cv2.imshow("MindfulMeal Vision", self._draw_overlay_ui(frame))
                    cv2.waitKey(1)
                except Exception:
                    pass
            try:
                # 编码 960 宽 (VISION_JPEG_WIDTH)：720p 下 resize+JPEG 共 ~3.5ms；
                # 运动分析用 320 宽（粗粒度运动足够），~0.5ms
                small = self._perception_small(frame, config.VISION_JPEG_WIDTH)
                jpeg = _frame_to_jpeg_bytes(small, quality=config.VISION_JPEG_QUALITY)
                if jpeg:
                    self._last_frame_jpeg = jpeg
                    frame_bytes, frame_fmt = jpeg, "jpeg"
                else:
                    frame_bytes, frame_fmt = _frame_to_bmp_bytes(small), "bmp"
                motion = self._analyze_motion(self._perception_small(frame, 320))
                mode_label = {"picamera": "real-picamera", "webcam": "real-webcam",
                              "mock": "mock"}.get(self._mode or "", self._mode)
                analysis = {"motion": motion, "eating_speed": {}}
            except Exception as exc:
                frame_bytes = frame_fmt = None
                analysis = {"motion": {}, "eating_speed": {}, "error": str(exc)}
                mode_label = "error"
            # fps 用「帧间隔」衡量：真实交付率（浏览器实际收到的帧率）
            now = time.time()
            if last_frame_at:
                frame_times.append(max(1e-6, now - last_frame_at))
                if len(frame_times) > 30:
                    frame_times.pop(0)
                avg = sum(frame_times) / len(frame_times)
                self._fps = 1.0 / avg if avg > 0 else 0.0
            last_frame_at = now
            self._frame_seq += 1
            with self._lock:
                self._latest = {
                    "mode": mode_label,
                    "timestamp": now,
                    "frame": frame_bytes,
                    "frame_format": frame_fmt,
                    "analysis": analysis,
                    "fps": round(self._fps, 1),
                    "seq": self._frame_seq,
                }
            # 上限保护：仅当相机快于 VISION_FRAME_FPS 时节流（本机 720p 30.7fps 不触发）
            spent = time.time() - now
            sleep_for = self.interval - spent
            if sleep_for > 0:
                time.sleep(sleep_for)

    # ---------- 手口线程 ----------
    _HAND_WINDOW_SEC = 30.0
    _CHEW_WINDOW_SEC = 60.0
    _HEAD_MOTION_THRESHOLD = _EATING["head_motion_threshold"]
    _HEAD_STABLE_COOLDOWN = 0.8
    _CHEW_THRESHOLD = 0.18
    _CHEW_COOLDOWN = 0.3
    _CHEW_PATTERN_THRESHOLD = 3
    _MOUTH_OPEN_SKIP = 0.02
    # 送食判定：指尖到嘴心距离 / 嘴宽。旧值是 0.35 且要求连续 2 次命中，
    # 而采样间隔比一次送食动作还长，几乎不可能被计到。
    # 现行默认 1.5 倍嘴宽，可在 models/eating_thresholds.json 里调。
    _BITE_DIST_RATIO_TH = _EATING["bite_dist_ratio"]
    _BITE_MIN_NEAR_SEC = 0.0      # 进入嘴部范围即可计（离开后才允许下一次）
    _BITE_LEAVE_SEC = _EATING["bite_leave_sec"]
    _BITE_REFRACTORY_SEC = _EATING["bite_refractory_sec"]
    _CHEW_FAST_TH = _EATING["chew_fast_threshold"]
    _CHEW_SLOW_TH = _EATING["chew_slow_threshold"]

    def _hand_loop(self) -> None:
        window_sec = self._HAND_WINDOW_SEC
        bites_tracker = BiteTracker(self._BITE_MIN_NEAR_SEC, self._BITE_LEAVE_SEC,
                                    self._BITE_REFRACTORY_SEC, window_sec)
        chew_records: list[float] = []
        last_chew_ts = 0.0
        mouth_openness_history: list[float] = []
        chewing_pattern_count = 0
        face_roi_history: list[tuple[float, float]] = []
        last_head_motion_time = 0.0
        head_motion = False
        while self._running:
            hand_result_out = {"note": "MediaPipe 手口/咀嚼检测（v2 算法）"}
            try:
                frame = self._latest_frame
                if frame is None:
                    self._hand_result = {"note": "等待画面…"}
                    time.sleep(self.hand_interval)
                    continue
                if not (self._ensure_hands() and self._ensure_face()):
                    self._hand_result = {"note": "mediapipe 未启用（模型缺失或库不兼容）"}
                    time.sleep(self.hand_interval)
                    continue
                now = time.time()
                # 内部帧为 BGR；MediaPipe 期望 RGB。仅此线程转换（0.5s 一次）
                import cv2
                # 降采样到 640 宽再做关键点：720p 全幅推理慢，且送食瞬间容易错过
                frame = self._perception_small(frame, 640)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = frame.shape[:2]
                hands, faces = self._detect_hands_face(frame)
                face_lms = faces[0] if faces else None
                if face_lms is not None:
                    # 嘴心取上下内唇中点（13/14）。旧实现用 61/291/0/17 的平均，
                    # 得到的是脸中心（鼻梁）而不是嘴，送食距离因此整体偏大。
                    mouth = ((face_lms[13].x + face_lms[14].x) / 2,
                             (face_lms[13].y + face_lms[14].y) / 2)
                    mouth_w = math.hypot((face_lms[61].x - face_lms[291].x) * w,
                                         (face_lms[61].y - face_lms[291].y) * h)
                    mouth_scale = max(mouth_w, w * 0.06)
                    lip_openness = abs((face_lms[15].y - face_lms[13].y) * h)
                    # ---- 头动过滤 ----
                    head_motion = False
                    fh = (face_lms[10].x * w, face_lms[10].y * h)
                    ch_pt = (face_lms[152].x * w, face_lms[152].y * h)
                    lc = (face_lms[234].x * w, face_lms[234].y * h)
                    rc = (face_lms[454].x * w, face_lms[454].y * h)
                    center = ((lc[0] + rc[0]) / 2, (fh[1] + ch_pt[1]) / 2)
                    fw = max(abs(rc[0] - lc[0]), 1.0)
                    fhgt = max(abs(ch_pt[1] - fh[1]), 1.0)
                    face_roi_history.append(center)
                    if len(face_roi_history) >= 3:
                        base = face_roi_history[-3]
                        xs = [abs(c[0] - base[0]) for c in face_roi_history[-2:]]
                        ys = [abs(c[1] - base[1]) for c in face_roi_history[-2:]]
                        xr = (sum(xs) / len(xs)) / fw * 100
                        yr = (sum(ys) / len(ys)) / fhgt * 100
                        head_motion = (xr > self._HEAD_MOTION_THRESHOLD
                                       or yr > self._HEAD_MOTION_THRESHOLD)
                        if head_motion:
                            last_head_motion_time = now
                            chewing_pattern_count = 0
                        if len(face_roi_history) > 5:
                            face_roi_history.pop(0)
                    # ---- 送食检测：指尖进入嘴部范围记一次；离开后才允许下一次 ----
                    min_ratio = float("inf")
                    for hand in hands:
                        tip = hand[8]
                        d = math.hypot((tip.x - mouth[0]) * w, (tip.y - mouth[1]) * h)
                        min_ratio = min(min_ratio, d / mouth_scale)
                    near = min_ratio < self._BITE_DIST_RATIO_TH
                    bite_trigger = bites_tracker.update(near, now)
                    bites = bites_tracker.bite_count
                    bpm = bites_tracker.per_min()
                    level = ("none" if bites == 0
                             else "fast" if bites > 12 else ("slow" if bites < 3 else "normal"))
                    # ---- 咀嚼速率（每秒输出，而非仅在触发瞬间） ----
                    chews = round(len(chew_records) / (self._CHEW_WINDOW_SEC / 60), 1)
                    chew_level = ("none" if chews == 0
                                  else "fast" if chews > self._CHEW_FAST_TH
                                  else ("slow" if chews < self._CHEW_SLOW_TH else "normal"))
                    chews_per_bite = (round(len(chew_records) / bites, 1)
                                      if bites else None)
                    chew_ok = (not head_motion
                               and (now - last_head_motion_time) >= self._HEAD_STABLE_COOLDOWN)
                    if chew_ok:
                        if lip_openness > max(3.0, h * self._MOUTH_OPEN_SKIP):
                            chewing_pattern_count = 0
                        else:
                            mouth_openness_history.append(lip_openness)
                            if len(mouth_openness_history) > 12:
                                mouth_openness_history.pop(0)
                            if len(mouth_openness_history) >= 5:
                                avg_o = sum(mouth_openness_history) / len(mouth_openness_history)
                                o_change = abs(lip_openness - avg_o) / avg_o if avg_o > 0 else 0
                                if (now - last_chew_ts) < self._CHEW_COOLDOWN:
                                    pass
                                elif o_change > self._CHEW_THRESHOLD:
                                    chewing_pattern_count += 1
                                else:
                                    chewing_pattern_count = max(0, chewing_pattern_count - 1)
                                if chewing_pattern_count >= self._CHEW_PATTERN_THRESHOLD:
                                    chew_records.append(now)
                                    last_chew_ts = now
                                    chewing_pattern_count = 0
                                    chew_records = [t for t in chew_records
                                                    if now - t <= self._CHEW_WINDOW_SEC]
                    hand_result_out.update({
                        "hands": len(hands),
                        "hand_near_mouth": bite_trigger,
                        "bite_count": bites,
                        "bites_per_min": bpm,
                        "level": level,
                        "chews_per_min": chews,
                        "chew_level": chew_level,
                        "chews_per_bite": chews_per_bite,
                        "head_motion": head_motion,
                        # 调试用：指尖与嘴心的距离/嘴宽，以及是否处于"贴近嘴"状态
                        "mouth_dist_ratio": (round(min_ratio, 2)
                                             if min_ratio != float("inf") else None),
                        "near_mouth": near,
                    })
                self._hand_result = hand_result_out
            except Exception:
                self._hand_result = {"note": "mediapipe 检测失败"}
            time.sleep(self.hand_interval)

    # ---------- 感知线程（PerceptionHub：情绪 + 肌肉法咀嚼） ----------
    def _perception_loop(self) -> None:
        try:
            self._perception_hub = PerceptionHub()
            self._perception_result = {"available": True, "note": "感知模型已就绪"}
        except Exception as e:
            self._perception_result = {"available": False, "reason": f"初始化失败: {e}"}
            return
        while self._running:
            if self._perception_suspended.is_set():
                time.sleep(0.05)      # 让路：不推理也不抢 GIL
                continue
            try:
                frame = self._latest_frame
                if frame is None:
                    time.sleep(0.1)
                    continue
                # 降采样后再推理：720p → 320x180，analyze_frame 228ms → 18ms
                # （内部帧已是 BGR，hub.analyze_frame 直接接收，零转换）
                small = self._perception_small(frame)
                self._perception_result = self._perception_hub.analyze_frame(small)
                self._maybe_log_emotion(self._perception_result)
                try:
                    self._perception_hub.write_snapshot(throttle=0.3)
                except Exception:
                    pass
            except Exception as e:
                self._perception_result = dict(self._perception_result or {})
                self._perception_result["error"] = f"{type(e).__name__}: {e}"
            time.sleep(self.perception_interval)

    # ---------- MediaPipe hands/face（tasks 优先，solutions 兜底） ----------
    def _detect_hands_face(self, frame: np.ndarray):
        """按各自实际的 API 风格调用：手与脸可能一个走 tasks、一个走 solutions。

        旧实现只在 `_hand_style == "tasks"` 时整体走 tasks 分支，混合风格时会对
        tasks 版的 FaceLandmarker 调 `.process()` 抛 AttributeError，被外层吞掉后
        表现为「手口检测失败 / 进食计数一直为 0」。
        """
        if self._hand_style == "tasks":
            mp_img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=frame)
            hands = self._hands.detect(mp_img).hand_landmarks or []
        else:
            res = self._hands.process(frame)
            hands = [h.landmark for h in (res.multi_hand_landmarks or [])]
        if self._face_style == "tasks":
            mp_img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=frame)
            faces = self._face.detect(mp_img).face_landmarks or []
        else:
            res = self._face.process(frame)
            faces = [f.landmark for f in (res.multi_face_landmarks or [])]
        return hands, faces

    def _ensure_hands(self) -> bool:
        if self._hands is not None:
            return True
        try:
            import mediapipe as mp
            if hasattr(mp, "solutions") and hasattr(mp.solutions, "hands"):
                self._hands = mp.solutions.hands.Hands(
                    static_image_mode=False, max_num_hands=2,
                    min_detection_confidence=0.5, min_tracking_confidence=0.5,
                )
                self._hand_style = "solutions"
                return True
        except Exception:
            pass
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision
            model_path = self._ensure_model("hand_landmarker.task")
            if model_path is None:
                return False
            options = vision.HandLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_path=str(model_path)), num_hands=2)
            self._hands = vision.HandLandmarker.create_from_options(options)
            self._hand_style = "tasks"
            self._mp = mp
            return True
        except Exception:
            return False

    def _ensure_face(self) -> bool:
        if self._face is not None:
            return True
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision
            model = self._ensure_model("face_landmarker.task")
            if model is None:
                return False
            options = vision.FaceLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_path=str(model)), num_faces=1)
            self._face = vision.FaceLandmarker.create_from_options(options)
            self._face_style = "tasks"
            self._mp = mp
            return True
        except Exception:
            pass
        try:
            import mediapipe as mp
            if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
                self._face = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=False, max_num_faces=1, refine_landmarks=True)
                self._face_style = "solutions"
                self._mp = mp
                return True
        except Exception:
            pass
        return False

    def _ensure_model(self, filename: str):
        """本地 models 目录查找；缺失时下载到本地。"""
        import urllib.request
        p = config.PATHS.models_dir / filename
        if p.exists():
            return p
        urls = {
            "hand_landmarker.task": ("https://storage.googleapis.com/mediapipe-models/"
                                     "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"),
            "face_landmarker.task": ("https://storage.googleapis.com/mediapipe-models/"
                                     "face_landmarker/face_landmarker/float16/1/face_landmarker.task"),
        }
        url = urls.get(filename)
        if not url:
            return None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(url, p)
            return p
        except Exception:
            return None

    # ---------- GLM-4V 视觉线程 ----------
    def _cloud_vision_due(self) -> bool:
        """现在该不该打云端视觉识别。

        模拟画面（没探测到摄像头时 `_frame_mock` 画的彩色条纹）**不打**：
        那张图里不可能有食物/人脸，打过去只是白花额度、还会把"彩色条纹"当场景写进卡片。
        真实摄像头恢复后（`_probe_mode` 每 10 秒重试）自动继续。
        """
        if self._mode == "mock":
            return False
        jpeg = self._last_frame_jpeg
        return bool(jpeg)

    def _llm_loop(self) -> None:
        while self._running:
            try:
                if self._cloud_vision_due():
                    self._llm_result = self._analyze_llm(self._last_frame_jpeg)
            except Exception:
                pass
            time.sleep(self.llm_interval)

    def _analyze_llm(self, jpeg_bytes: bytes) -> dict:
        import requests
        from ..core.ai_config import AIConfigStore
        cfg = AIConfigStore().load()
        # 视觉用 vision_* 那组连接信息（没单独配才沿用对话的）：
        # 对话换成百炼 Qwen 之后，视觉仍留在智谱，不能把 glm-4v 发到 Qwen 的端点上
        api_url, api_key = cfg.vision_connection if cfg else ("", "")
        if not (api_url and api_key):
            return {"face": {"note": "AI 视觉未配置（本地表情见\"实时感知\"卡片）"},
                    "distract": {"note": "AI 视觉未配置，无法识别分心物"},
                    "food": {"note": "AI 视觉未配置，无法识别食物"},
                    "emotion": {}, "scene": "", "_ts": None}
        if not api_key:
            return {"face": {"note": "AI Key 缺失"}, "distract": {}, "food": {},
                    "emotion": {}, "scene": "", "_ts": None}
        model = cfg.vision_model or "glm-4v-flash"
        prompt = (
            "你是正念进食助手的视觉分析模块。分析这张餐桌/人物照片，只返回 JSON（不要其它文字）："
            '{"face":{"face_count":整数,"expression":"开心/平静/悲伤/专注/其它"},'
            '"distract":{"objects":["cell phone","laptop","tablet"]},'
            '"food":{"description":"画面中的食物中文描述","residual_estimate":"多/中/少/无",'
            '"items":[{"name":"食物中文名","portion_g":估计重量克数,'
            '"kcal":这一份的千卡,'
            '"protein_g":蛋白质克数,"carb_g":碳水克数,"fat_g":脂肪克数}],'
            '"kcal_total":整盘合计千卡,'
            '"nutrition":"一句话营养点评（偏高油/偏高糖/蔬菜不足/搭配均衡之类）"},'
            '"emotion":{"label":"中性/开心/悲伤/专注"},"scene":"用餐中/餐前/餐后/其它",'
            '"summary":"一句中文场景描述"}'
        )
        try:
            resp = requests.post(
                api_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/jpeg;base64,"
                                               + base64.b64encode(jpeg_bytes).decode("ascii")}},
                        ],
                    }],
                    "temperature": 0.2,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = _extract_json(resp.json()["choices"][0]["message"]["content"])
            data["food"] = self._with_nutrition(data.get("food") or {})
            data["_ts"] = time.time()
            return data
        except Exception as exc:
            return {"face": {"note": f"GLM-4V 分析失败: {exc}"}, "distract": {}, "food": {},
                    "emotion": {}, "scene": "", "_ts": None}

    @staticmethod
    def _with_nutrition(food: dict) -> dict:
        """把视觉模型给的食物条目补上卡路里/能量/营养，并算出整盘合计。

        模型只负责"看到什么、大概多少克"；热量优先用内置 55 种中餐热量库
        （命中就用库里的 kcal/100g × 克数重算，比模型自己报的数字稳），
        没命中才用模型的估算，同时在条目上标明来源（db / model）。
        """
        from .food import enrich_food_items, food_totals

        items = enrich_food_items(food.get("items") or food.get("foods"))
        if not items:
            # 旧格式（只有 description + calorie_estimate_kcal）也兼容：至少保住总热量
            kcal = food.get("calorie_estimate_kcal") or food.get("kcal_total")
            if kcal:
                try:
                    kcal = int(round(float(kcal)))
                    food["kcal_total"] = kcal
                    food["energy_kj"] = int(round(kcal * 4.184))
                except (TypeError, ValueError):
                    pass
            return food
        totals = food_totals(items)
        food["items"] = items
        food["kcal_total"] = totals["kcal"]
        food["energy_kj"] = totals["energy_kj"]
        food["carb_g"], food["protein_g"], food["fat_g"] = (
            totals["carb_g"], totals["protein_g"], totals["fat_g"])
        # 兼容既有前端/卡片字段
        food["calorie_estimate_kcal"] = totals["kcal"]
        return food

    # ---------- 摄像头 ----------
    def _release_camera(self) -> None:
        if self._picam is not None:
            try:
                self._picam.stop()
            except Exception:
                pass
            self._picam = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._prev_gray = None

    def _probe_mode(self) -> str:
        if sys.platform.startswith("linux"):
            try:
                from picamera2 import Picamera2  # noqa: F401
                return "picamera"
            except Exception:
                pass
        try:
            import cv2
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(0)
            ok = cap.isOpened()
            cap.release()
            if ok:
                return "webcam"
        except Exception:
            pass
        return "mock"

    def _capture_picamera(self) -> np.ndarray:
        from picamera2 import Picamera2
        if self._picam is None:
            self._picam = Picamera2()
            cfg = self._picam.create_video_configuration(main={"size": (320, 240), "format": "RGB888"})
            self._picam.configure(cfg)
            self._picam.start()
            time.sleep(0.4)
        arr = self._picam.capture_array()
        # 统一转 BGR（cv2 原生）；320x240 转换开销可忽略
        try:
            import cv2
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception:
            return arr

    def _capture_webcam(self) -> np.ndarray:
        import cv2
        if self._cap is None:
            self._cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(0)
            # 720p 采集保证画面清晰；编码/感知用降采样版，避免 720p JPEG 编码 47ms 卡主循环
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self._cap.set(cv2.CAP_PROP_FPS, 30)
            if not self._cap.isOpened():
                raise RuntimeError("webcam open failed")
        ret, bgr = self._cap.read()
        if not ret or bgr is None:
            raise RuntimeError("webcam read failed")
        return bgr  # 保持 BGR（cv2 原生），编码/运动分析零转换

    # ---------- 摄像头情绪落库（健康分析的情绪分布要用） ----------
    _EMOTION_LOG_MIN_GAP = 20.0      # 最快 20 秒记一条，避免刷爆数据库
    _EMOTION_LOG_REPEAT_GAP = 300.0  # 情绪没变也至少 5 分钟记一条

    def _maybe_log_emotion(self, snap: dict) -> None:
        emo = (snap or {}).get("emotion") or {}
        if not emo.get("face_count"):
            return
        key = str(emo.get("emotion") or emo.get("emotion_zh") or "")
        if not key:
            return
        now = time.time()
        if now - self._last_emotion_log_ts < self._EMOTION_LOG_MIN_GAP:
            return
        if (key == self._last_logged_emotion
                and now - self._last_emotion_log_ts < self._EMOTION_LOG_REPEAT_GAP):
            return
        label = emo.get("emotion_zh") or key
        payload = {
            "user_id": "default",
            "trigger_ts": now,
            "source": "camera",
            "emotion": key,
            "emotion_label": label,
            "emotion_zh": label,
            "confidence": emo.get("confidence"),
            "valence": emo.get("valence"),
            "arousal": emo.get("arousal"),
            "emotion_data": {
                "source": "camera",
                "face_list": [{"emotion": key, "emotion_label": label,
                               "confidence": emo.get("confidence")}],
            },
        }
        try:
            from ..core.db import HealthStore
            HealthStore().add_event("emotion_detect", payload, status="local")
            self._last_logged_emotion = key
            self._last_emotion_log_ts = now
        except Exception:
            pass

    @staticmethod
    def _perception_small(frame: np.ndarray, target_w: int = 320) -> np.ndarray:
        """感知/编码输入降采样：
        - 感知默认 320 宽：720p 推理 228ms → 18ms（-92%）
        - 编码用 640 宽：720p JPEG 47ms → 13ms，避免卡主循环
        """
        import cv2
        if frame is None or frame.shape[1] <= target_w:
            return frame
        nh = max(1, int(frame.shape[0] * target_w / frame.shape[1]))
        return cv2.resize(frame, (target_w, nh))

    def _analyze_motion(self, frame: np.ndarray) -> dict:
        try:
            import cv2
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            if self._prev_gray is None:
                self._prev_gray = gray
                return {"active": False, "motion_ratio": 0.0}
            diff = cv2.absdiff(self._prev_gray, gray)
            motion_mask = cv2.threshold(diff, config.MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
            ratio = float(cv2.countNonZero(motion_mask)) / (gray.shape[0] * gray.shape[1])
            self._prev_gray = gray
            return {"active": ratio > 0.02, "motion_ratio": round(ratio, 3)}
        except Exception:
            return {"active": False, "motion_ratio": 0.0}

    # ---------- Mock ----------
    def _frame_mock(self) -> np.ndarray:
        h, w = 240, 320
        t = time.time()
        x = np.linspace(0, 2 * np.pi, w)
        phase = t * 1.6
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        for i, ch in enumerate((0, 1, 2)):
            wave = 118 + 110 * np.sin(x * (1 + i * 0.35) + phase + i * 2.1)
            frame[:, :, ch] = np.tile(wave.astype(np.uint8), (h, 1))
        bx = int((w - 40) * (0.5 + 0.5 * np.sin(phase * 0.7)))
        by = int((h - 40) * (0.5 + 0.5 * np.cos(phase * 0.5)))
        frame[by:by + 40, bx:bx + 40] = [255, 255, 255]
        frame[-22:, :] = [10, 10, 20]
        return frame

    def _analysis_mock(self) -> dict:
        t = time.time()
        rnd = random.Random(int(t * 10))
        bite = rnd.randint(0, 16)
        level = "normal" if 3 <= bite <= 12 else ("fast" if bite > 12 else "slow")
        chews = rnd.randint(6, 40)
        chew_level = "normal" if 15 <= chews <= 30 else ("fast" if chews > 30 else "slow")
        return {
            "motion": {"active": bool(int(t) % 2 == 0), "motion_ratio": round(rnd.uniform(0.0, 0.55), 2)},
            "eating_speed": {"bites_per_min": round(bite / 0.5, 1), "level": level},
            "hand_mouth": {"hand_near_mouth": rnd.random() < 0.35, "hands": rnd.randint(0, 1)},
            "chewing": {"chews_per_min": float(chews), "chew_level": chew_level,
                        "chews_per_bite": round(chews / max(bite, 1), 1)},
        }


# 全局单例：感知模型全进程只加载一份
vision_service = VisionService()
