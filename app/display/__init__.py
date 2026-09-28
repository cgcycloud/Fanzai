"""设备小屏显示层：小屏页面渲染 + 屏幕输出 + 渲染服务。"""
from .service import screen_service, ScreenService
from .state import (FACE_IDLE, FACE_LISTENING, FACE_SPEAKING, FACE_THINKING,
                    PAGE_TITLES, PAGES, DisplayState, display_state)

__all__ = [
    "screen_service", "ScreenService", "display_state", "DisplayState",
    "PAGES", "PAGE_TITLES", "FACE_IDLE", "FACE_LISTENING",
    "FACE_THINKING", "FACE_SPEAKING",
]
