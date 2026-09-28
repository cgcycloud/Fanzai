"""GC9A01 圆形屏（240x240，软件 SPI 与 ReSpeaker 共存）+ 正念状态显示。"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


class GC9A01:
    """软件 SPI 驱动（lgpio）。仅树莓派可初始化。"""

    def __init__(self):
        try:
            import lgpio
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "lgpio is required on Raspberry Pi. Install with: sudo apt install python3-lgpio") from exc
        cfg = config.DISPLAY_CONFIG
        self._lgpio = lgpio
        self.mosi, self.sclk, self.cs, self.dc, self.rst = (
            cfg.din_gpio, cfg.clk_gpio, cfg.cs_gpio, cfg.dc_gpio, cfg.rst_gpio)
        self.width, self.height = cfg.width, cfg.height
        self.gpio = lgpio.gpiochip_open(0)
        for pin in (self.mosi, self.sclk, self.cs, self.dc, self.rst):
            self._lgpio.gpio_claim_output(self.gpio, pin, 0)
        self._lgpio.gpio_write(self.gpio, self.cs, 1)

    def write(self, values):
        self._lgpio.gpio_write(self.gpio, self.cs, 0)
        for value in values:
            for bit in range(7, -1, -1):
                self._lgpio.gpio_write(self.gpio, self.mosi, (value >> bit) & 1)
                self._lgpio.gpio_write(self.gpio, self.sclk, 1)
                self._lgpio.gpio_write(self.gpio, self.sclk, 0)
        self._lgpio.gpio_write(self.gpio, self.cs, 1)

    def command(self, value, data=()):
        self._lgpio.gpio_write(self.gpio, self.dc, 0)
        self.write((value,))
        if data:
            self._lgpio.gpio_write(self.gpio, self.dc, 1)
            self.write(data)

    def reset(self):
        self._lgpio.gpio_write(self.gpio, self.rst, 1)
        time.sleep(0.05)
        self._lgpio.gpio_write(self.gpio, self.rst, 0)
        time.sleep(0.05)
        self._lgpio.gpio_write(self.gpio, self.rst, 1)
        time.sleep(0.12)

    def initialize(self):
        self.reset()
        init_commands = (
            (0xEF, ()), (0xEB, (0x14,)), (0xFE, ()), (0xEF, ()),
            (0xEB, (0x14,)), (0x84, (0x40,)), (0x85, (0xFF,)),
            (0x86, (0xFF,)), (0x87, (0xFF,)), (0x88, (0x0A,)),
            (0x89, (0x21,)), (0x8A, (0x00,)), (0x8B, (0x80,)),
            (0x8C, (0x01,)), (0x8D, (0x01,)), (0x8E, (0xFF,)),
            (0x8F, (0xFF,)), (0xB6, (0x00, 0x20)), (0x36, (0x08,)),
            (0x3A, (0x05,)), (0x90, (0x08, 0x08, 0x08, 0x08)),
            (0xBD, (0x06,)), (0xBC, (0x00,)), (0xFF, (0x60, 0x01, 0x04)),
            (0xC3, (0x13,)), (0xC4, (0x13,)), (0xC9, (0x22,)),
            (0xBE, (0x11,)), (0xE1, (0x10, 0x0E)),
            (0xDF, (0x21, 0x0C, 0x02)),
            (0xF0, (0x45, 0x09, 0x08, 0x08, 0x26, 0x2A)),
            (0xF1, (0x43, 0x70, 0x72, 0x36, 0x37, 0x6F)),
            (0xF2, (0x45, 0x09, 0x08, 0x08, 0x26, 0x2A)),
            (0xF3, (0x43, 0x70, 0x72, 0x36, 0x37, 0x6F)),
            (0xED, (0x1B, 0x0B)), (0xAE, (0x77,)), (0xCD, (0x63,)),
            (0x70, (0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03)),
            (0xE8, (0x34,)),
            (0x62, (0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70)),
            (0x63, (0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70)),
            (0x64, (0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07)),
            (0x66, (0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00)),
            (0x67, (0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98)),
            (0x74, (0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00)),
            (0x98, (0x3E, 0x07)), (0x35, ()), (0x21, ()), (0x11, ()),
        )
        for command, data in init_commands:
            self.command(command, data)
        time.sleep(0.12)
        self.command(0x29)
        time.sleep(0.02)

    def _window(self):
        self.command(0x2A, (0, 0, 0, self.width - 1))
        self.command(0x2B, (0, 0, 0, self.height - 1))

    def fill(self, red: int, green: int, blue: int) -> None:
        self._window()
        pixel = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        data = [pixel >> 8, pixel & 0xFF] * (self.width * self.height)
        self._lgpio.gpio_write(self.gpio, self.dc, 0)
        self.write((0x2C,))
        self._lgpio.gpio_write(self.gpio, self.dc, 1)
        self.write(data)


COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "purple": (128, 0, 128), "orange": (255, 165, 0),
}


class MindfulDisplay:
    """正念助手圆屏：状态色 + emoji 文案（无字体渲染时以纯色+日志提示）。"""

    STATUS_COLORS = {
        "idle": "black", "boot": "green", "wake": "green", "listening": "blue",
        "thinking": "yellow", "speaking": "white", "error": "red", "sleep": "black",
        "happy": "green", "sad": "blue", "meditate": "purple", "eating": "orange",
        "warning": "yellow", "success": "green",
    }

    def __init__(self, display_config=None, auto_init: bool = True):
        self.config = display_config or config.DISPLAY_CONFIG
        self._initialized = False
        self.lcd: Optional[GC9A01] = None
        self._current_status = "idle"
        if auto_init:
            self.init()

    def init(self) -> bool:
        try:
            self.lcd = GC9A01()
            self.lcd.initialize()
            self._initialized = True
            logger.info("圆屏初始化成功")
            return True
        except Exception as e:
            logger.error("圆屏初始化失败: %s", e)
            self._initialized = False
            return False

    @property
    def is_ready(self) -> bool:
        return self._initialized and self.lcd is not None

    def show_color(self, color_name: str = "black") -> None:
        if not self.is_ready:
            return
        r, g, b = COLORS.get(color_name, (0, 0, 0))
        self.lcd.fill(r, g, b)

    def show_status(self, status: str) -> None:
        self._current_status = status
        self.show_color(self.STATUS_COLORS.get(status, "black"))

    def boot(self) -> None:
        self.show_status("boot")

    def off(self) -> None:
        self.show_color("black")
