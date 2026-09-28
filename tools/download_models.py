"""下载语音识别模型（sherpa-onnx Paraformer 中文）。

用法: .venv/Scripts/python -m tools.download_models
唤醒(KWS)与合成(VITS)模型见 tools/download_sherpa_models.py
"""
from __future__ import annotations

import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config

MIRROR = "https://hf-mirror.com/csukuangfj/sherpa-onnx-paraformer-zh-small-2024-03-09/resolve/main"
FILES = ["model.int8.onnx", "tokens.txt"]


def fetch(url: str, out: Path) -> bool:
    if out.exists() and out.stat().st_size > 1000:
        print(f"  已存在 {out.name}")
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        print(f"  ✅ {out.name} ({out.stat().st_size / 1e6:.1f}MB)")
        return True
    except Exception as exc:
        print(f"  ❌ {out.name}: {exc}")
        return False


def main() -> None:
    dst = config.PATHS.models_dir / config.SHERPA_MODEL_DIRNAME
    print(f"下载 Paraformer 中文模型 → {dst}")
    ok = all(fetch(f"{MIRROR}/{n}", dst / n) for n in FILES)
    print("\n完成" if ok else "\n部分失败：识别将不可用，请检查网络后重试")
    print("提示：唤醒与合成模型请运行 python tools/download_sherpa_models.py")


if __name__ == "__main__":
    main()

