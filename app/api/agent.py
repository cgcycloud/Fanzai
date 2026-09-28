"""交互设置 API —— 第一页下滑面板：模式 / 自主互动 / 亮度 / 音量 / 语音唤醒 + 主动互动推送。

设置改动会立刻落到硬件：切换模式时开/关摄像头，音量下发到系统（树莓派）。
主动互动由 app/core/autonomy.py 产生，经 /api/agent/proactive 以 SSE 推给设备页。
语音唤醒由浏览器持续上传 PCM，这里用 sherpa-onnx KWS 关键词模型判定（见 app/voice/sherpa_kws.py）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import queue
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..core import interaction_config as ic
from ..core.autonomy import autonomy_service
from ..voice.sherpa_kws import kws_detector

router = APIRouter(prefix="/api/agent")

# 改这些字段需要让唤醒识别器按新关键词重建
_WAKE_KEYS = ("wake_enabled", "wake_mode", "wake_words")


def active_wake_detector():
    """唤醒引擎：sherpa-onnx KWS（中文关键词专用模型）。

    旧的 Vosk 唤醒方案已删除（词表里没有「崽」，走语法限制永远无法命中，
    自由识别又会把「饭崽」听成「贩灾」）。模型缺失时这里返回 KWS 检测器，
    其 feed() 会返回 reason="no-model"，前端据此提示去下载模型。
    """
    return kws_detector


def _drop_wake_sessions() -> None:
    try:
        kws_detector.drop_all()
    except Exception:
        pass


def apply_settings(settings=None) -> dict:
    """把当前设置落到运行时：模式决定摄像头，音量下发系统。"""
    settings = settings or ic.get_settings()
    try:
        from ..vision.service import vision_service
        if settings.vision_enabled:
            vision_service.start()
        else:
            vision_service.stop()
    except Exception as exc:
        print(f"[agent] 摄像头切换失败: {exc}", flush=True)
    _apply_volume(settings.volume)
    return settings.public_dict()


def _apply_volume(percent: int) -> None:
    """树莓派上把音量下发给 ALSA；其它平台忽略（浏览器端自行设置播放音量）。"""
    if not sys.platform.startswith("linux"):
        return
    try:
        subprocess.run(["amixer", "sset", "PCM", f"{int(percent)}%"],
                       check=False, timeout=3, capture_output=True)
    except Exception:
        pass


@router.get("/settings")
async def get_settings():
    return ic.panel_payload()


@router.post("/settings")
async def set_settings(request: Request):
    body = await request.json()
    changes = {k: body[k] for k in
               ("mode", "autonomy", "tts_enabled", "brightness", "volume", "speech_rate",
                *_WAKE_KEYS) if k in body}
    settings = ic.update_settings(**changes)
    apply_settings(settings)
    # 唤醒词/开关变了 → 丢弃现存识别器，下次上传用新关键词重建
    if any(k in changes for k in _WAKE_KEYS):
        _drop_wake_sessions()
    return {"ok": True, "settings": settings.public_dict()}


@router.get("/status")
async def agent_status():
    return {"settings": ic.get_settings().public_dict(), "autonomy": autonomy_service.status()}


# ===================== 语音唤醒 =====================

def _log_wake_event(result: dict, words: list[str]) -> None:
    """唤醒命中落库（与旧的树莓派监听器保持同一种事件，便于统一统计）。"""
    try:
        from ..core.db import HealthStore
        HealthStore().add_event("wakeword", {
            "event": "wakeword",
            "wake_words": words,
            "matched_word": result.get("word"),
            "match_text": result.get("text"),
            "confidence": result.get("confidence"),
            "trigger_ts": time.time(),
        }, status="local")
    except Exception:
        pass


@router.get("/wake")
async def wake_status():
    settings = ic.get_settings()
    detector = active_wake_detector()
    status = detector.status()
    return {
        "enabled": settings.wake_enabled,
        "mode": settings.wake_mode,
        "mode_name": settings.wake_mode_info["name"],
        "words": list(settings.wake_words),
        "ack_text": ic.WAKE["ack_text"],
        "chunk_ms": ic.WAKE["chunk_ms"],
        "gate_silence": bool(ic.WAKE.get("gate_silence", True)),
        "engine": status.get("engine", "sherpa-kws"),
        "detector": status,
    }


def _wake_ack_audio() -> tuple[bytes | None, str]:
    """唤醒应答语音（「我在」）：优先 TTS 缓存，未命中才现场合成并回填缓存。

    合成走云端 Qwen3-TTS（1~2 秒），是阻塞操作，所以调用方要放进工作线程；
    启动时已预热，正常情况下这里是纯内存读取。

    未命中时交给 `tts_cache.prewarm_ack`：它会挑一条"收干净了"的结果。
    短句最容易被 TTS 赶着收尾、尾音掐断，而唤醒应答每次唤醒都要播，
    一旦把残句预热进缓存，用户每次唤醒都会听到同一句没说完的"我在"。
    """
    from ..voice import tts_cache

    text = str(ic.WAKE["ack_text"] or "").strip()
    if not text:
        return None, ""
    from ..voice.tts import cache_voice
    voice = cache_voice()          # 必须与预热时用的键一致，否则缓存读不到
    cached = tts_cache.get(text, voice)
    if cached:
        return cached, text
    try:
        data = tts_cache.prewarm_ack(text, voice)
    except Exception as exc:
        print(f"[wake] 应答语音合成失败: {exc}", flush=True)
        return None, text
    return (data, text) if data else (None, text)


@router.get("/wake/ack")
async def wake_ack():
    """唤醒应答语音（base64）：设备页命中唤醒词后先回应一声「我在」，再开始收音。"""
    if not ic.tts_enabled():
        return {"ok": False, "text": "", "muted": True}      # 关了语音：不出声，直接进入聆听
    data, text = await asyncio.to_thread(_wake_ack_audio)
    if not data:
        return {"ok": False, "text": text}
    from ..voice.tts import detect_format
    return {"ok": True, "text": text, "format": detect_format(data),
            "audio_b64": base64.b64encode(data).decode("ascii")}


@router.post("/wake/feed")
async def wake_feed(request: Request):
    """浏览器上传一段 16k 单声道 S16_LE PCM，返回是否命中唤醒词。

    识别是 CPU 阻塞操作，放工作线程，避免卡住 MJPEG 流与事件循环。
    """
    settings = ic.get_settings()
    if not settings.wake_enabled:
        return {"matched": False, "enabled": False}
    pcm = await request.body()
    session_id = request.query_params.get("session") or "default"
    if not pcm:
        return {"matched": False, "text": "", "confidence": 0.0}

    # 调试钩子（默认关闭）：把浏览器上传的原始 PCM 追加落盘，用于排查"传了却识别不出"。
    # 用原始字节追加（wave 模块不支持追加模式），事后套个 WAV 头即可分析。
    import os as _os
    if _os.environ.get("MINDFUL_WAKE_DEBUG") == "1":
        try:
            import numpy as _np
            from .. import config as _cfg
            _dbg = _cfg.PATHS.data_dir / "wake_debug.pcm"
            _dbg.parent.mkdir(parents=True, exist_ok=True)
            with open(_dbg, "ab") as _f:
                _f.write(pcm)
            _a = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
            print(f"[wake-dbg] chunk={len(pcm)}B peak={float(_np.max(_np.abs(_a))):.3f} "
                  f"rms={float(_np.sqrt(_np.mean(_a ** 2))):.4f}", flush=True)
        except Exception as _exc:
            print(f"[wake-dbg] 落盘失败: {_exc}", flush=True)

    detector = active_wake_detector()
    try:
        result = await asyncio.to_thread(
            detector.feed, session_id, pcm,
            words=list(settings.wake_words),
            cooldown_sec=ic.WAKE["cooldown_sec"],
            max_hits_per_min=ic.WAKE["max_hits_per_min"],
        )
    except Exception as exc:
        return {"matched": False, "text": "", "confidence": 0.0, "error": str(exc)}

    result["enabled"] = True
    result["engine"] = detector.status().get("engine", "sherpa-kws")
    if result.get("matched"):
        _log_wake_event(result, list(settings.wake_words))
        # 硬件设备屏同步进入「倾听」（浏览器端自己也会切表情）
        try:
            from ..display.service import screen_service
            screen_service.on_listening()
        except Exception:
            pass
    return result


@router.post("/wake/reset")
async def wake_reset(request: Request):
    """丢弃会话识别器（页面卸载/切换模式/手动重开监听时调用）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session") or request.query_params.get("session") or "default")
    try:
        kws_detector.drop_all() if body.get("all") else kws_detector.drop(session_id)
    except Exception:
        pass
    return {"ok": True}


@router.get("/proactive")
async def proactive_stream(request: Request):
    """SSE：AI 主动互动事件（文本 + 语音 + 表情）。"""
    q = autonomy_service.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.to_thread(q.get, True, 20.0)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield ("event: proactive\ndata: "
                       + json.dumps(event, ensure_ascii=False) + "\n\n")
        finally:
            autonomy_service.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
