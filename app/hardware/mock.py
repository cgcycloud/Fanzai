"""Mock 硬件 —— 无树莓派/无外设时本地演示可用。

MockCameraIO: 从 test_images/ 依次取图模拟拍照
MockAudioIO : 从 test_audio/ 依次取 wav 模拟录音；播放只打印
MockButtonWatcher: 键盘 1/2/3 模拟唤醒/确认/跳过按键
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import config

logger = logging.getLogger(__name__)


class MockCameraIO:
    def __init__(self, media_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.media_dir = media_dir
        self.store = store
        self.image_dir = Path("test_images")
        self.image_files = sorted(
            list(self.image_dir.glob("*.jpg")) + list(self.image_dir.glob("*.png"))
            + list(self.image_dir.glob("*.jpeg")))
        self.index = 0

    def photo(self, output: Path, width: int = 1280, height: int = 720) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not self.image_files:
            output.touch()
            return output
        src = self.image_files[self.index % len(self.image_files)]
        self.index += 1
        shutil.copy(src, output)
        if self.store is not None and hasattr(self.store, "add_media"):
            self.store.add_media("photo", output, {"width": width, "height": height, "mock": True})
        return output


class MockAudioIO:
    def __init__(self, audio_config: Optional[Any] = None,
                 media_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.audio_config = audio_config
        self.media_dir = media_dir
        self.store = store
        self.audio_dir = Path("test_audio")
        self.record_files = sorted(self.audio_dir.glob("*.wav"))
        self.index = 0
        self._volume_gain = 1.0

    def record(self, output: Path, seconds: Optional[int] = None) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not self.record_files:
            output.touch()
            return output
        src = self.record_files[self.index % len(self.record_files)]
        self.index += 1
        shutil.copy(src, output)
        if self.store is not None and hasattr(self.store, "add_media"):
            self.store.add_media("audio", output, {"seconds": seconds, "mock": True})
        return output

    def play(self, file_path: Path) -> None:
        print(f"MockAudio play: {file_path}")

    def set_volume_gain(self, gain: float) -> None:
        self._volume_gain = gain

    def analyze_voice(self, wav_path: Path) -> Dict[str, Any]:
        return {"emotion": "neutral", "speech_rate": 120.0, "pause_count": 2,
                "confidence": 0.75, "pitch_f0": 180.0, "speech_rate_chars_per_minute": 120}


class MockButtonWatcher:
    """键盘 1=唤醒 2=确认 3=跳过（需 pip install keyboard；缺失时只提示）。"""

    def __init__(self, button_config: Optional[Any] = None, store: Optional[Any] = None):
        self.button_config = button_config or config.BUTTON_CONFIG
        self.store = store
        self._callbacks: Dict[str, List[Callable]] = {"wakeup": [], "confirm": [], "skip": []}
        self._running = False

    def on_wakeup(self, cb: Callable) -> None:
        self._callbacks["wakeup"].append(cb)

    def on_confirm(self, cb: Callable) -> None:
        self._callbacks["confirm"].append(cb)

    def on_skip(self, cb: Callable) -> None:
        self._callbacks["skip"].append(cb)

    def _emit(self, name: str) -> None:
        for cb in self._callbacks[name]:
            cb()

    def listen(self) -> None:
        try:
            import keyboard
        except ImportError:
            print("MockButtonWatcher: 未安装 keyboard 库，按键监听不可用（pip install keyboard）")
            return
        key_map = {"1": "wakeup", "2": "confirm", "3": "skip"}
        print("MockButtonWatcher: 按 1=唤醒 2=确认 3=跳过，Ctrl+C 退出")
        self._running = True
        for key, name in key_map.items():
            keyboard.add_hotkey(key, lambda n=name: self._emit(n))
        try:
            while self._running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass


def detect_hardware() -> dict:
    return {
        "camera": {"ok": False, "output": "mock mode (no real camera)"},
        "playback": {"ok": False, "output": "mock mode (silent)"},
        "capture": {"ok": False, "output": "mock mode (test_audio files)"},
        "gpio": {"ok": False, "output": "mock mode (keyboard 1/2/3)"},
        "reSpeaker": {"ok": False, "output": "mock mode"},
    }
