"""视觉 API —— 快照轮询 / MJPEG 视频流 / 启停 / 表情设置 / 实时感知。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from ..vision.service import vision_service

router = APIRouter(prefix="/api/vision")


@router.post("/start")
async def start_vision():
    return vision_service.start()


@router.post("/stop")
async def stop_vision():
    return vision_service.stop()


@router.get("/status")
async def vision_status():
    return vision_service.status()


@router.get("/snapshot")
async def vision_snapshot():
    return vision_service.snapshot()


@router.post("/emotion")
async def set_robot_emotion(request_body: dict):
    emo = str(request_body.get("emotion") or "neutral")
    vision_service.set_robot_emotion(emo)
    return {"ok": True, "emotion": vision_service.get_robot_emotion()}


@router.get("/emotion")
async def get_robot_emotion():
    return {"ok": True, "emotion": vision_service.get_robot_emotion()}


@router.get("/stream")
async def video_stream():
    """MJPEG 视频流（浏览器 <img src> 直接可用）。

    直接取原始 JPEG 字节 + 帧序号去重，像 /api/device/stream 一样零 base64
    往返、零 analysis 构建 —— 旧实现每帧调用 snapshot()（复制大字典）并
    base64 解码，这正是页面卡顿、帧率上不去的主要原因。
    """
    boundary = "frame"

    async def gen():
        last_seq = -1
        while True:
            data, fmt, seq, _ts = vision_service.latest_stream_frame()
            if data and seq != last_seq:
                last_seq = seq
                mime = "image/jpeg" if fmt == "jpeg" else "image/bmp"
                yield (f"--{boundary}\r\nContent-Type: {mime}\r\n"
                       f"Content-Length: {len(data)}\r\n\r\n").encode("ascii") + data + b"\r\n"
            else:
                # 没有新帧时也短暂让出，避免空转烧 CPU
                await asyncio.sleep(0.02)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.get("/live")
async def live_perception():
    """实时感知快照（本地情绪+咀嚼，来自 live_snapshot.json）。"""
    import json as _json
    from .. import config
    p = config.PATHS.data_dir / "live_snapshot.json"
    if p.exists():
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"available": False, "reason": "暂无实时感知数据（请启动摄像头）"}


@router.get("/frame")
async def latest_frame():
    """最新一帧 JPEG（非流式，供低带宽场景）。"""
    data = vision_service.latest_jpeg()
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})
