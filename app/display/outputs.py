"""屏幕输出后端 —— 同一份渲染结果，三种出口。

  1. fbdev  直接写 /dev/fbN（树莓派接 SPI 屏并启用 fbtft 驱动时最省事，推荐）
  2. spi    直接驱动 ST7789/ILI9341（需要 spidev，不依赖内核驱动）
  3. null   无硬件：只把最新一帧留在内存里，供浏览器镜像查看（Windows 开发用）

树莓派 2.8 寸屏（240x320）通常是 ILI9341 或 ST7789。两块屏的初始化序列不同，
本模块按配置选择；未接硬件时选 null 即可，界面逻辑完全一致。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from .. import config

logger = logging.getLogger(__name__)


def to_rgb565_bytes(img: Image.Image) -> bytes:
    """PIL 图 → RGB565 大端字节流（SPI 屏与 framebuffer 通用）。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[:, :, 0] & 0xF8) << 8
    g = (arr[:, :, 1] & 0xFC) << 3
    b = (arr[:, :, 2] & 0xF8) >> 3
    return (r | g | b).astype(">u2").tobytes()


class ScreenOutput:
    """输出后端基类。"""

    name = "base"

    def show(self, img: Image.Image) -> None:      # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullOutput(ScreenOutput):
    """无硬件：不做任何输出（渲染结果仍可通过 Web 镜像查看）。"""

    name = "null"

    def show(self, img: Image.Image) -> None:
        return


class FbdevOutput(ScreenOutput):
    """直接写 Linux framebuffer（/dev/fb1 等，fbtft 驱动的小屏）。"""

    name = "fbdev"

    def __init__(self, fb_path: str, width: int, height: int):
        self.width, self.height = width, height
        self.fb_path = fb_path
        self._fb = open(fb_path, "r+b", buffering=0)
        logger.info("屏幕输出: framebuffer %s (%dx%d)", fb_path, width, height)

    def show(self, img: Image.Image) -> None:
        data = to_rgb565_bytes(img.resize((self.width, self.height)))
        self._fb.seek(0)
        self._fb.write(data)

    def close(self) -> None:
        try:
            self._fb.close()
        except Exception:
            pass


class SpiLcdOutput(ScreenOutput):
    """直接驱动 ST7789 / ILI9341（spidev + GPIO 控制 DC/RST）。"""

    name = "spi"

    # ST7789 (240x320) 初始化序列
    _ST7789_INIT = [
        (0x01, None), (0x11, None), (0x3A, [0x55]), (0x36, [0x00]),
        (0x21, None), (0x13, None), (0x29, None),
    ]
    # ILI9341 (240x320) 初始化序列
    _ILI9341_INIT = [
        (0x01, None), (0x28, None), (0x3A, [0x55]), (0x36, [0x48]),
        (0xC0, [0x23]), (0xC1, [0x10]),
        (0xC5, [0x3E, 0x28]), (0xC7, [0x86]),
        (0xB1, [0x00, 0x18]), (0xB6, [0x08, 0x82, 0x27]),
        (0x11, None), (0x29, None),
    ]

    def __init__(self, controller: str = "st7789", width: int = 240, height: int = 320,
                 bus: int = 0, device: int = 0, speed_hz: int = 40_000_000,
                 dc_gpio: int = 25, rst_gpio: int = 27, offset_x: int = 0, offset_y: int = 0):
        try:
            import spidev
        except ModuleNotFoundError as exc:
            raise RuntimeError("spi 输出需要 spidev：sudo apt install python3-spidev") from exc
        try:
            import lgpio
        except ModuleNotFoundError as exc:
            raise RuntimeError("spi 输出需要 lgpio：sudo apt install python3-lgpio") from exc

        self.width, self.height = width, height
        self.offset_x, self.offset_y = offset_x, offset_y
        self._lgpio = lgpio
        self._gpio = lgpio.gpiochip_open(0)
        self.dc, self.rst = dc_gpio, rst_gpio
        for pin in (self.dc, self.rst):
            lgpio.gpio_claim_output(self._gpio, pin, 0)

        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

        self._reset()
        init_seq = self._ILI9341_INIT if controller.lower().startswith("ili") else self._ST7789_INIT
        for cmd, data in init_seq:
            self._cmd(cmd, data)
            time.sleep(0.02 if cmd in (0x01, 0x11, 0x29) else 0.005)
        logger.info("屏幕输出: SPI %s %dx%d", controller, width, height)

    # ---------- 底层 ----------
    def _cmd(self, cmd: int, data: Optional[list] = None) -> None:
        self._lgpio.gpio_write(self._gpio, self.dc, 0)
        self.spi.writebytes([cmd])
        if data:
            self._lgpio.gpio_write(self._gpio, self.dc, 1)
            self.spi.writebytes(data)

    def _reset(self) -> None:
        self._lgpio.gpio_write(self._gpio, self.rst, 1)
        time.sleep(0.05)
        self._lgpio.gpio_write(self._gpio, self.rst, 0)
        time.sleep(0.05)
        self._lgpio.gpio_write(self._gpio, self.rst, 1)
        time.sleep(0.12)

    def _window(self) -> None:
        x0, y0 = self.offset_x, self.offset_y
        x1, y1 = x0 + self.width - 1, y0 + self.height - 1
        self._cmd(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._cmd(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self._cmd(0x2C)

    def show(self, img: Image.Image) -> None:
        data = to_rgb565_bytes(img.resize((self.width, self.height)))
        self._window()
        self._lgpio.gpio_write(self._gpio, self.dc, 1)
        # 分块写，避免单次 transfer 过大被内核限制
        chunk = 4096
        for i in range(0, len(data), chunk):
            self.spi.writebytes2(data[i:i + chunk])

    def close(self) -> None:
        try:
            self.spi.close()
        except Exception:
            pass
        try:
            self._lgpio.gpiochip_close(self._gpio)
        except Exception:
            pass


def create_output() -> ScreenOutput:
    """按配置/平台选择输出后端；失败一律降级为 null（界面仍可用 Web 镜像看）。"""
    mode = (config.SCREEN_OUTPUT or "auto").lower()
    if mode in ("auto", "fbdev"):
        fb = config.SCREEN_FBDEV
        if sys.platform.startswith("linux") and Path(fb).exists():
            try:
                return FbdevOutput(fb, config.SCREEN_W, config.SCREEN_H)
            except Exception as exc:
                logger.warning("framebuffer 输出不可用(%s)，降级", exc)
        if mode == "fbdev":
            logger.warning("未找到 %s，降级为无输出", fb)
    if mode == "spi":
        try:
            return SpiLcdOutput(
                controller=config.SCREEN_SPI_CONTROLLER,
                width=config.SCREEN_W, height=config.SCREEN_H,
                dc_gpio=config.SCREEN_SPI_DC_GPIO, rst_gpio=config.SCREEN_SPI_RST_GPIO,
                offset_x=config.SCREEN_SPI_OFFSET_X, offset_y=config.SCREEN_SPI_OFFSET_Y,
            )
        except Exception as exc:
            logger.warning("SPI 输出不可用(%s)，降级为无输出", exc)
    return NullOutput()
