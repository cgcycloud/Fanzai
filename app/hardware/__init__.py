"""硬件层自动探测：真机（Linux + arecord/aplay）优先，否则 Mock 回退。

选择规则：
  1. 环境变量 MINDFUL_FORCE_MOCK=1        → 强制 Mock
  2. hardware_real 可导入（树莓派）        → 真机
  3. 其它（Windows / 依赖缺失）            → Mock（本地演示可用）
"""
from __future__ import annotations

import logging
import os

from .. import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
__version__ = "2.0.0"


def _load_hardware():
    if not os.environ.get("MINDFUL_FORCE_MOCK"):
        try:
            from .real import AudioIO, ButtonWatcher, CameraIO, detect_hardware
            logger.info("Real hardware loaded (Raspberry Pi mode).")
            return AudioIO, CameraIO, ButtonWatcher, detect_hardware
        except Exception as exc:
            logger.info("Real hardware unavailable (%s), falling back to Mock.", exc)
    from .mock import MockAudioIO as AudioIO, MockCameraIO as CameraIO, \
        MockButtonWatcher as ButtonWatcher, detect_hardware
    logger.info("Mock hardware loaded (local/dev mode).")
    return AudioIO, CameraIO, ButtonWatcher, detect_hardware


AudioIO, CameraIO, ButtonWatcher, detect_hardware = _load_hardware()

__all__ = ["AudioIO", "CameraIO", "ButtonWatcher", "detect_hardware", "__version__"]
