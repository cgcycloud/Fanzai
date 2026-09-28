"""设备屏幕 API —— 屏幕镜像、页面切换、表情状态。

浏览器的"设备屏幕"页会持续拉 /api/device/screen.png 显示设备真实画面，
因此无需树莓派硬件也能在开发机上确认小屏排版。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..display.service import screen_service
from ..display.state import FACE_LISTENING, FACE_SPEAKING, PAGE_TITLES, PAGES

router = APIRouter(prefix="/api/device")


@router.get("/status")
async def status():
    return screen_service.status()


@router.get("/stream")
async def screen_stream():
    """MJPEG 实时流：浏览器 <img src> 即可，无需轮询（解决刷新率低的问题）。"""
    import asyncio

    async def gen():
        last_seq = -1
        while True:
            data, seq = screen_service.jpeg()
            screen_service.mark_stream_seen()
            if data and seq != last_seq:
                last_seq = seq
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
            # 没有新帧时不发送任何 part（发空 part 会让浏览器解析失败断流）
            await asyncio.sleep(0.02)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.post("/scroll")
async def scroll(request: Request):
    """上下滚动详细页内容（delta 为像素，正=向下）。"""
    body = await request.json()
    page = str(body.get("page") or screen_service.status()["page"])
    delta = float(body.get("delta") or 0)
    pos = screen_service.state.add_scroll(page, delta)
    return {"ok": True, "page": page, "scroll": round(pos, 1)}


@router.get("/screen.png")
async def screen_png():
    """当前屏幕内容（PNG）。no-store 保证浏览器每次都拿新帧。"""
    data = screen_service.png()
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/start")
async def start():
    return screen_service.start()


@router.post("/stop")
async def stop():
    return screen_service.stop()


@router.get("/page")
async def get_page():
    return {"page": screen_service.status()["page"], "pages": PAGES, "titles": PAGE_TITLES}


@router.post("/page")
async def set_page(request: Request):
    body = await request.json()
    page = str(body.get("page") or "").strip()
    if page == "next":
        return {"ok": True, "page": screen_service.next_page()}
    if page not in PAGES:
        return {"ok": False, "error": f"page 必须是 {PAGES} 之一", "page": screen_service.status()["page"]}
    return {"ok": True, "page": screen_service.set_page(page)}


@router.get("/state")
async def get_state():
    """设备屏当前完整状态（调试用）。"""
    from ..display.state import display_state
    return display_state.snapshot()


@router.post("/turn")
async def push_turn(request: Request):
    """把一轮对话推给设备屏（前端在收到 done 事件时调用；服务端路径会自动推送）。"""
    body = await request.json()
    screen_service.on_turn(str(body.get("user") or ""), str(body.get("assistant") or ""))
    return {"ok": True}


@router.post("/notice")
async def notice(request: Request):
    """在设备屏上显示一条短提示（几秒后自动消失）。"""
    import threading

    body = await request.json()
    text = str(body.get("text") or "")[:40]
    seconds = float(body.get("seconds") or 4.0)
    screen_service.state.set_status_line(text)

    def _clear():
        import time as _t
        _t.sleep(max(1.0, seconds))
        if screen_service.state.status_line() == text:
            screen_service.state.set_status_line("")

    threading.Thread(target=_clear, daemon=True).start()
    return {"ok": True}


@router.post("/listen")
async def listen_state(request: Request):
    """忙碌状态上报：前端在「录音 / 播报 / 生成」时置忙，空闲时解除。

    这是**防止 AI 主动开口打断用户**的关键一环：录音还没上传时服务端
    完全不知道有人在说话，若不用这个标志挡一下，用户话说到一半就会被
    自主互动插嘴。body.reason 只用于区分表情与诊断。
    """
    body = await request.json()
    active = bool(body.get("active"))
    reason = str(body.get("reason") or "listening")
    screen_service.state.set_listening(active)
    if active:
        if reason in ("speaking", "ack"):
            screen_service.state.set_face(FACE_SPEAKING)
        else:
            screen_service.state.set_face(FACE_LISTENING)
        screen_service.state.set_status_line("正在听你说…" if reason == "listening" else "")
    else:
        screen_service.on_idle()
    return {"ok": True, "busy": screen_service.state.is_busy(),
            "face": screen_service.status()["face"]}


