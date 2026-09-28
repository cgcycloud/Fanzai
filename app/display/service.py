"""设备屏幕服务 —— 后台渲染线程 + 与语音/感知联动的表情状态。

职责：
  1. 以固定帧率渲染当前页面并推给屏幕输出（framebuffer / SPI / 无硬件）
  2. 维护"表情随语音变化"的状态机：
       待机(idle，四种表情轮换) → 倾听(listening) → 思考(thinking) → 说话(speaking) → 待机
  3. 定期从感知服务刷新指标（咀嚼/进食/情绪）与健康数据（看板页）
  4. 保留最新一帧 PNG，供浏览器镜像查看（Windows 无硬件也能看到真实画面）

对外接口（供 API 层调用）：
    screen_service.start() / stop() / status()
    screen_service.png()          → 最新帧 PNG 字节
    screen_service.set_page()     → 切换页面，返回当前页
    screen_service.next_page()
"""
from __future__ import annotations

import base64
import io
import threading
import time
from typing import Optional

from PIL import Image, ImageEnhance

from .. import config
from ..core.interaction_config import get_settings
from . import render as render_mod
from .outputs import create_output
from .state import (FACE_LISTENING, FACE_SPEAKING, FACE_THINKING,
                    PAGES, DisplayState, display_state)


class ScreenService:
    def __init__(self, state: Optional[DisplayState] = None):
        self.state = state or display_state
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._output = None
        self._png: bytes = b""
        self._jpeg: bytes = b""
        self._jpeg_ts = 0.0
        self._frame_seq = 0
        self._stream_ts = 0.0          # 最近一次有浏览器拉流的时间
        self._frames = 0
        self._fps = 0.0
        self._last_render_at = 0.0
        self._png_ts = 0.0
        self._camera_jpeg_raw: bytes | None = None    # 已解码的那一帧原始字节（避免重复解码）
        self._last_metrics = 0.0
        self._last_stats = 0.0
        self._buttons_started = False
        self._stats: dict = {}
        self._analysis: dict = {}
        self._camera_frame: Optional[Image.Image] = None

    # ---------- 生命周期 ----------
    def start(self) -> dict:
        if self._running:
            return self.status()
        self._running = True
        self._output = create_output()
        self._thread = threading.Thread(target=self._loop, name="device-screen", daemon=True)
        self._thread.start()
        self._start_watchdog()
        return self.status()

    def stop(self) -> dict:
        self._running = False
        if self._output:
            try:
                self._output.close()
            except Exception:
                pass
        return self.status()

    def status(self) -> dict:
        return {
            "running": self._running,
            "page": self.state.get_page(),
            "face": self.state.face_state(),
            "output": getattr(self._output, "name", "none"),
            "fps": round(self._fps, 1),
            "frames": self._frames,
            "size": [config.SCREEN_W, config.SCREEN_H],
        }

    # ---------- 页面 ----------
    def set_page(self, page: str) -> str:
        p = self.state.set_page(page)
        return p

    def next_page(self) -> str:
        return self.set_page(self.state.next_page())

    def png(self) -> bytes:
        with self._lock:
            return self._png

    def jpeg(self) -> tuple[bytes, int]:
        """(最新 JPEG, 帧序号)。序号用于流端判断是否新帧（长度可能碰巧相同）。"""
        with self._lock:
            return self._jpeg, self._frame_seq

    def mark_stream_seen(self) -> None:
        """浏览器正在拉 MJPEG 流：保持 JPEG 编码与全速渲染。"""
        self._stream_ts = time.time()

    # ---------- 表情状态（供对话/音频流程调用）----------
    def on_listening(self) -> None:
        self.state.set_listening(True)          # 忙标志：AI 不得在此刻主动插话
        self.state.set_face(FACE_LISTENING)
        self.state.set_status_line("正在听你说…")

    def on_thinking(self) -> None:
        self.state.set_face(FACE_THINKING)
        self.state.set_status_line("我在想…")

    def on_speaking(self, est_seconds: float = 4.0) -> None:
        self.state.mark_speaking(est_seconds)
        self.state.set_status_line("")

    def on_idle(self) -> None:
        self.state.set_listening(False)
        self.state.end_speaking()
        self.state.set_status_line("")

    def on_turn(self, user: str, assistant: str) -> None:
        """记录一轮对话（只保留最近两轮），并让表情带上情感。"""
        self.state.add_turn(user, assistant)

    def on_emotion(self, emotion: str) -> None:
        self.state.set_emotion(emotion)

    # ---------- 树莓派按键切页 ----------
    def start_buttons(self) -> bool:
        """启动 GPIO 按键监听：跳过键切页。仅树莓派可用，失败返回 False。"""
        if self._buttons_started:
            return True
        try:
            from gpiozero import Button
        except Exception:
            return False
        try:
            btn = Button(config.BUTTON_CONFIG.skip_gpio, pull_up=True,
                         bounce_time=config.BUTTON_CONFIG.bounce_time)
            btn.when_pressed = self._on_page_button
            self._buttons_started = True
            print(f"[screen] 按键切页已启用（GPIO{config.BUTTON_CONFIG.skip_gpio}）", flush=True)
            return True
        except Exception as exc:
            print(f"[screen] 按键不可用: {exc}", flush=True)
            return False

    def _on_page_button(self) -> None:
        page = self.next_page()
        print(f"[screen] 按键切页 → {page}", flush=True)

    # ---------- 渲染循环 ----------
    def _refresh_metrics(self) -> None:
        """刷新分析指标；摄像头 JPEG 在渲染循环中走 latest_jpeg 轻量路径。"""
        try:
            from ..vision.service import vision_service
            snap = vision_service.snapshot()
            vision_on = vision_service.is_running()
            a = snap.get("analysis") or {}
            self._analysis = a
            chew = a.get("chewing") or {}
            eat = a.get("eating_speed") or {}
            p = (a.get("perception") or {}).get("emotion") or {}
            face_count = p.get("face_count")
            if vision_on:
                self.state.set_metrics(
                    chews_per_min=chew.get("chews_per_min"),
                    chew_level=chew.get("chew_level"),
                    chew_count=chew.get("chew_count"),
                    bites_per_min=eat.get("bites_per_min"),
                    eat_level=eat.get("level"),
                    motion=(a.get("motion") or {}).get("active"),
                    # 情绪只取感知结果：设备屏第一页/第二页读的是同一份数值
                    emotion=(p.get("state_zh") or p.get("emotion_zh")) if face_count else None,
                    emotion_key=p.get("emotion") if face_count else None,
                    face_count=face_count,
                    vision=True,
                    metrics_ts=round(time.time(), 1),
                )
            else:
                # 摄像头关闭（日常聊天模式）：清空感知数值，避免两页显示过期数据
                self.state.set_metrics(chews_per_min=None, chew_level=None, chew_count=None,
                                       bites_per_min=None, eat_level=None, motion=None,
                                       emotion=None, emotion_key=None, face_count=None,
                                       vision=False, metrics_ts=round(time.time(), 1))
        except Exception:
            pass

    def _refresh_stats(self) -> None:
        """看板数据来自 analytics（有 DB 查询，30 秒刷一次即可）。"""
        try:
            from ..core.analytics import health_analyzer
            self._stats = health_analyzer()
        except Exception:
            self._stats = {}

    def _render_status(self) -> dict:
        """给设置页用的运行状态（引擎/输出/帧率）。"""
        try:
            from ..voice.asr import active_engine
            engine = active_engine()
        except Exception:
            engine = "—"
        return {"engine": engine, "output": getattr(self._output, "name", "none"),
                "fps": round(self._fps, 1)}

    def _start_watchdog(self) -> None:
        """看门狗：渲染线程冻结时转储全部线程堆栈并自动重建（自愈）。"""
        import faulthandler
        import io as _io

        def watch():
            stale = 0
            while True:
                time.sleep(3)
                with self._lock:
                    age = time.time() - self._last_render_at
                if age < 5:
                    stale = 0
                    continue
                stale += 1
                print(f"[screen] ⚠ 渲染线程 {age:.0f}s 未更新（stale={stale}），转储堆栈", flush=True)
                buf = _io.StringIO()
                faulthandler.dump_traceback(file=buf)
                print(buf.getvalue()[:4000], flush=True)
                if stale >= 2:
                    print("[screen] 重建渲染线程…", flush=True)
                    self._running = False
                    th = threading.Thread(target=self._loop, name="device-screen", daemon=True)
                    th.start()
                    print("[screen] 渲染线程已重建", flush=True)
                    return

        threading.Thread(target=watch, name="screen-watchdog", daemon=True).start()

    def _loop(self) -> None:
        interval = 1.0 / max(1, config.SCREEN_FPS)
        frame_times: list[float] = []
        last_frame_at = 0.0
        while self._running:
            t0 = time.time()
            try:
                page = self.state.get_page()
                show_camera = page == "camera"
                # 分析指标每 0.5s 刷新；视频帧每次循环只取原始 JPEG，避免重复构建 snapshot
                if t0 - self._last_metrics > 0.5:
                    self._refresh_metrics()
                    self._last_metrics = t0
                if page == "stats" and t0 - self._last_stats > 30:
                    self._refresh_stats()
                    self._last_stats = t0

                # 渲染策略（按"谁在看"决定刷新率，见 config 注释）：
                #   * 有真实小屏 → 按页给满帧（表情页 30fps、摄像头页 10fps）
                #   * 只有浏览器在拉 MJPEG 流 → 同样按页给帧（它就是"屏幕"）
                #   * 没人看 → 压到 SCREEN_IDLE_FPS，别白烧 CPU
                #   * 纯数据页 → 滚动/数据变化时刷，另有 0.4s 兜底
                watching = (self._output is not None) or (t0 - self._stream_ts < 2.0)
                if not watching:
                    fps = float(getattr(config, "SCREEN_IDLE_FPS", 2.5))
                elif page == "camera":
                    fps = float(getattr(config, "SCREEN_CAMERA_FPS", 10))
                else:
                    fps = float(config.SCREEN_FPS)
                due = ((t0 - self._last_render_at) >= (1.0 / max(1.0, fps))
                       or self.state.scroll_dirty
                       or (page not in ("face", "camera")
                           and (t0 - self._last_render_at) >= 0.4))
                # 摄像头帧**只在真要渲染时才解码**，而且同一帧不重复解码：
                # 以前是每个循环（50 次/秒）都 latest_jpeg() + Image.open()，
                # 每次新建图像对象、攒到 GC 才回收 —— 实测摄像头页因此多烧 ~2 核，
                # 跟"_stream_ts 没被用上"一起，就是"一进摄像头页就卡"的成因。
                if show_camera and due:
                    from ..vision.service import vision_service
                    jpeg = vision_service.latest_jpeg()
                    if jpeg and jpeg is not self._camera_jpeg_raw:
                        self._camera_jpeg_raw = jpeg
                        self._camera_frame = Image.open(io.BytesIO(jpeg))
                if due:
                    img, _content_h = render_mod.render(
                        self.state, t=t0,
                        frame=self._camera_frame if show_camera else None,
                        analysis=self._analysis,
                        stats=self._stats,
                        status=self._render_status(),
                    )
                    # 亮度调节（下滑面板）：只影响输出帧，不改变渲染逻辑
                    brightness = get_settings().brightness
                    if brightness < 100:
                        img = ImageEnhance.Brightness(img).enhance(brightness / 100.0)
                    self._last_render_at = t0
                    if self._output is not None:
                        self._output.show(img)
                    # JPEG 只给 MJPEG 流用：没人拉流就不编（2ms/帧，白编也是烧 CPU）
                    if t0 - self._stream_ts < 2.0:
                        jbuf = io.BytesIO()
                        img.save(jbuf, format="JPEG", quality=config.SCREEN_JPEG_QUALITY)
                        self._frame_seq += 1
                        with self._lock:
                            self._jpeg = jbuf.getvalue()
                            self._jpeg_ts = t0
                    # PNG 低频更新（兼容 screen.png 端点）
                    if t0 - self._png_ts > 0.5:
                        pbuf = io.BytesIO()
                        img.save(pbuf, format="PNG")
                        self._png = pbuf.getvalue()
                        self._png_ts = t0
                    self._frames += 1
            except Exception:
                import traceback
                print("[screen] 渲染异常:", flush=True)
                traceback.print_exc()

            # fps 用「帧间隔」衡量（用渲染耗时算会得出几百的假值）
            now = time.time()
            if last_frame_at:
                frame_times.append(max(1e-6, now - last_frame_at))
                if len(frame_times) > 30:
                    frame_times.pop(0)
                avg = sum(frame_times) / len(frame_times)
                self._fps = 1.0 / avg if avg > 0 else 0.0
            last_frame_at = now
            sleep_for = max(0.0, interval - (time.time() - t0))
            if sleep_for:
                time.sleep(sleep_for)


screen_service = ScreenService()
