"""音频 API —— 本地 sherpa ASR（/api/asr）+ TTS（/api/tts）+ 语音特征。

上传兼容两种方式：
  1. multipart/form-data（字段名 file）—— 标准上传
  2. 原始二进制 body（前端 MediaRecorder 直接 PUT 16k WAV）—— 零拷贝快路径
"""
from __future__ import annotations

import asyncio
import base64
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from ..core.db import HealthStore
from ..voice.asr import active_engine, transcribe_wav
from ..voice.convert import to_16k_wav
from ..voice.features import analyze_voice, record_speech_rate

router = APIRouter(prefix="/api")


async def _read_audio(request: Request) -> tuple[bytes, str]:
    """从请求中取出音频字节 + 文件名（multipart 或原始 body 都支持）。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        for key in ("file", "audio", "upload"):
            item = form.get(key)
            if item is not None and hasattr(item, "read"):
                return await item.read(), getattr(item, "filename", "") or "rec.wav"
        raise ValueError("multipart 中缺少 file 字段")
    return await request.body(), request.headers.get("x-filename", "rec.wav")


def _transcribe_blocking(wav_bytes: bytes, tmp) -> tuple[str, float]:
    """同步识别（在工作线程里跑，避免阻塞事件循环）。"""
    tmp.write_bytes(wav_bytes)
    return transcribe_wav(tmp)


def _wav_seconds(data: bytes) -> float:
    """16k 单声道 WAV 的时长（秒）；解析失败返回 0。"""
    import io
    import wave as _wave
    try:
        with _wave.open(io.BytesIO(data), "rb") as wav:
            return wav.getnframes() / max(wav.getframerate(), 1)
    except Exception:
        return 0.0


@router.post("/asr")
async def asr(request: Request):
    """语音转文字：录音 → 16k 单声道 WAV → 本地离线识别（Paraformer 优先）。

    识别是 CPU 密集的阻塞操作，必须放到工作线程：既避免卡住事件循环
    （否则 MJPEG 视频流与其他请求都会停顿），也让出 GIL 给识别本身。
    """
    t_start = time.time()
    try:
        raw, filename = await _read_audio(request)
    except Exception as exc:
        return JSONResponse({"ok": False, "text": "", "error": f"读取上传失败: {exc}"}, status_code=400)
    if not raw:
        return JSONResponse({"ok": False, "text": "", "error": "空音频"}, status_code=400)

    tmp = config.PATHS.media_dir / "audio" / f"asr-{int(time.time() * 1000)}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    suspended = False
    # 设备屏：识别中显示「思考」（用户已说完，正在理解）
    try:
        from ..display.service import screen_service
        screen_service.on_thinking()
    except Exception:
        pass
    try:
        wav_bytes = to_16k_wav(raw)          # 统一转成识别引擎要求的格式

        # 识别期间让视觉让路：感知线程暂停，避免 CPU/GIL 争抢（实测可差 4 倍）
        if config.YIELD_VISION_DURING_ASR:
            try:
                from ..vision.service import vision_service
                vision_service.suspend_perception()
                suspended = True
            except Exception:
                pass

        text, conf = await asyncio.to_thread(_transcribe_blocking, wav_bytes, tmp)
        # 每次说话都记录语速：健康分析的「平均语速」与语速情绪分布都依赖它
        try:
            await asyncio.to_thread(record_speech_rate, text, _wav_seconds(wav_bytes),
                                    HealthStore())
        except Exception:
            pass
        return {"ok": True, "text": text, "confidence": round(conf, 3),
                "engine": active_engine(), "elapsed_ms": int((time.time() - t_start) * 1000),
                "bytes": len(raw), "filename": filename}
    except Exception as exc:
        return JSONResponse({"ok": False, "text": "", "error": str(exc)}, status_code=200)
    finally:
        if suspended:
            try:
                from ..vision.service import vision_service
                vision_service.resume_perception()
            except Exception:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/tts")
async def tts(request: Request):
    """文本转语音：返回 base64 音频（Qwen3-TTS-Flash，失败由上层给提示音）。"""
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text 不能为空"}, status_code=400)
    from ..voice.tts import detect_format
    from ..voice.tts import synthesize
    out = config.PATHS.media_dir / "audio" / f"tts-{int(time.time() * 1000)}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    ok = synthesize(text, out)
    if not ok or not out.exists():
        # 把引擎记录的真实原因带出去：否则前端只看到"合成失败"，
        # 端点写错/Key 地域不对这类问题完全没法自己查。
        detail = ""
        try:
            from ..voice import qwen_tts_engine
            st = qwen_tts_engine.status()
            detail = str(st.get("last_error") or "")
            if not detail and not st.get("configured"):
                detail = "未配置 TTS API Key（管理后台 → 🔊 语音合成）"
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": "TTS 合成失败", "detail": detail},
                            status_code=500)
    raw = out.read_bytes()
    fmt = detect_format(raw)          # Qwen3-TTS 出 WAV；按实际字节判断更稳
    b64 = base64.b64encode(raw).decode("ascii")
    out.unlink(missing_ok=True)
    return {"ok": True, "audio_b64": b64, "format": fmt}


@router.post("/voice-analyze")
async def voice_analyze(request: Request):
    """语音特征分析（语速字/分钟），落库 wakeword 事件（供风险预警使用）。"""
    try:
        raw, _ = await _read_audio(request)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"读取上传失败: {exc}"}, status_code=400)
    tmp = config.PATHS.media_dir / "audio" / f"va-{int(time.time() * 1000)}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(to_16k_wav(raw))
        result = analyze_voice(tmp, store=HealthStore())
        result["ok"] = True
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        tmp.unlink(missing_ok=True)
    return result
