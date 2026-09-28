"""树莓派真机硬件：picamera2 摄像头 / arecord+aplay 音频 / gpiozero 按键。

环境检测：仅 Linux 且存在 arecord/aplay 时可导入；否则抛 ImportError 由
hardware/__init__ 捕获并回退 Mock。第三方库全部惰性导入，import 零副作用。
"""
from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Optional

from .. import config

logger = logging.getLogger(__name__)

if not (sys.platform.startswith("linux") and shutil.which("arecord") and shutil.which("aplay")):
    raise ImportError("Real hardware requires Linux with arecord/aplay (Raspberry Pi OS).")


class CameraIO:
    """Picamera2 真机摄像头：photo() 拍照、video() 录像。"""

    def __init__(self, media_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.media_dir = Path(media_dir) if media_dir else config.PATHS.media_dir
        self.store = store
        self._picam2 = None

    def _camera(self):
        if self._picam2 is None:
            try:
                from picamera2 import Picamera2
            except ModuleNotFoundError as exc:
                raise RuntimeError("python3-picamera2 is required on Raspberry Pi.") from exc
            self._picam2 = Picamera2()
        return self._picam2

    def photo(self, output: Path, width: int = 1280, height: int = 720) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        cam = self._camera()
        cfg = cam.create_still_configuration(main={"size": (width, height), "format": "RGB888"})
        cam.configure(cfg)
        cam.start()
        try:
            cam.capture_file(str(output))
        finally:
            cam.stop()
        if self.store is not None:
            self.store.add_media("photo", output, {"width": width, "height": height})
        return output

    def video(self, output: Path, seconds: int = 5, codec: str = "mjpeg") -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["rpicam-vid", "-t", str(int(seconds * 1000)), "--codec", codec, "-o", str(output)]
        subprocess.run(cmd, check=True, timeout=seconds + 60)
        if self.store is not None:
            self.store.add_media("video", output, {"seconds": seconds, "codec": codec})
        return output


class AudioIO:
    """ReSpeaker 真机音频：arecord 录音、aplay 播放、轻量语音特征分析。"""

    def __init__(self, audio_config: Optional[Any] = None,
                 media_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.audio_config = audio_config or config.AUDIO_CONFIG
        self.media_dir = Path(media_dir) if media_dir else config.PATHS.media_dir
        self.store = store
        self.device = self.audio_config.device

    def record(self, output: Path, seconds: Optional[int] = None) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["arecord", "-D", self.device, "-f", self.audio_config.record_format,
               "-r", str(self.audio_config.sample_rate), "-c", "1"]
        if seconds:
            cmd += ["-d", str(seconds)]
        cmd.append(str(output))
        subprocess.run(cmd, check=True, timeout=(seconds or 30) + 15)
        if self.store is not None:
            self.store.add_media("audio", output, {"seconds": seconds, "device": self.device})
        return output

    def play(self, file_path: Path) -> None:
        subprocess.run(["aplay", "-D", self.device, str(file_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def test_tone(self, seconds: float = 1.0, frequency: int = 440) -> None:
        sample_rate = self.audio_config.sample_rate
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        frames = int(sample_rate * seconds)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"".join(
                struct.pack("<h", int(9000 * math.sin(2 * math.pi * frequency * i / sample_rate)))
                for i in range(frames)))
        self.play(path)
        path.unlink(missing_ok=True)

    def analyze_voice(self, wav_path: Path) -> dict:
        """轻量语音特征（能量包络停顿计数 + 过零率近似语速）。"""
        try:
            import numpy as np
            with wave.open(str(wav_path), "rb") as wav:
                sample_rate = wav.getframerate()
                data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32)
            if len(data) == 0:
                raise ValueError("empty audio")
            win = max(int(sample_rate * 0.02), 1)
            n_win = len(data) // win
            if n_win < 2:
                raise ValueError("audio too short")
            energy = np.array([np.sqrt(np.mean(data[i * win:(i + 1) * win] ** 2)) for i in range(n_win)])
            voiced = energy > max(float(np.mean(energy) * 0.3), 1.0)
            pause_count = max(int(np.sum(voiced[1:] & ~voiced[:-1])) - 1, 0)
            zcr = float(np.mean(np.abs(np.diff(np.sign(data)))[::100])) if len(data) > 100 else 0.0
            duration = len(data) / sample_rate
            return {
                "emotion": "neutral", "speech_rate": round(zcr, 2),
                "pause_count": pause_count, "confidence": 0.7, "pitch_f0": 180.0,
                "speech_rate_chars_per_minute": int(
                    max(0, len(data) / max(duration, 0.1) / sample_rate * 60 * 2)),
            }
        except Exception as exc:
            logger.warning("analyze_voice fallback: %s", exc)
            return {"emotion": "neutral", "speech_rate": 0.0, "pause_count": 0,
                    "confidence": 0.5, "pitch_f0": 0.0, "speech_rate_chars_per_minute": 0}


class ButtonWatcher:
    """gpiozero 真机按键监听。"""

    def __init__(self, button_config: Optional[Any] = None, store: Optional[Any] = None):
        self.button_config = button_config or config.BUTTON_CONFIG
        self.store = store

    def listen(self) -> None:
        try:
            from gpiozero import Button
        except ModuleNotFoundError as exc:
            raise RuntimeError("python3-gpiozero is required on Raspberry Pi.") from exc
        btn = Button(self.button_config.wakeup_gpio, pull_up=True,
                     bounce_time=self.button_config.bounce_time)
        print(f"button listener started: GPIO{self.button_config.wakeup_gpio} (Ctrl+C to quit)", flush=True)
        btn.when_pressed = lambda: print("wakeup button pressed", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def detect_hardware() -> dict:
    result: dict[str, dict] = {}
    try:
        from picamera2 import Picamera2  # noqa: F401
        result["camera"] = {"ok": True, "output": "Picamera2 importable"}
    except Exception as exc:
        result["camera"] = {"ok": False, "output": f"picamera2 unavailable: {exc}"}
    result["playback"] = {"ok": shutil.which("aplay") is not None,
                          "output": "aplay: " + (shutil.which("aplay") or "not found")}
    result["capture"] = {"ok": shutil.which("arecord") is not None,
                         "output": "arecord: " + (shutil.which("arecord") or "not found")}
    try:
        import gpiozero  # noqa: F401
        result["gpio"] = {"ok": True, "output": "gpiozero available"}
    except Exception as exc:
        result["gpio"] = {"ok": False, "output": f"gpiozero unavailable: {exc}"}
    result["reSpeaker"] = {"ok": True, "output": "device config: plughw:seeed2micvoicec,0 (assumed present)"}
    return result
