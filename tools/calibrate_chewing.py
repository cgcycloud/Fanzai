"""咀嚼标定 / 重新训练的启动入口（供 train_chewing.bat 双击调用）。

等价于：.venv\\Scripts\\python.exe -m app.vision.chewing_engine --calibrate
用导入方式调用可以避免 `-m` 触发的重复导入警告。
不带参数时默认进入标定模式；`--detect` / `--demo` / `--help` 原样透传。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.vision.chewing_engine import main  # noqa: E402


if __name__ == "__main__":
    known = {"--calibrate", "--detect", "--demo", "-h", "--help"}
    if not any(arg in known for arg in sys.argv[1:]):
        sys.argv.append("--calibrate")
    main()
