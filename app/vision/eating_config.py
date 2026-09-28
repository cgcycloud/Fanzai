"""进食判定参数 —— 想调灵敏度改这里，不用动代码。

参数文件：`models/eating_thresholds.json`（不存在时自动按默认值生成一份）

  bite_dist_ratio        指尖到嘴心距离 ÷ 嘴宽 的门槛；越小越难判成"送到嘴边"
                         （探针里的 mouth_dist_ratio 常年大于它就说明手离嘴太远）
  bite_leave_sec         手离开嘴边多久之后，才允许计入下一次送食（去抖）
  bite_refractory_sec    两次送食之间的最短间隔（防止一口被算成好几口）
  chew_fast_threshold    咀嚼多快算"偏快"（次/分）
  chew_slow_threshold    咀嚼多慢算"偏慢"（次/分）
  chew_prob_threshold   咀嚼概率门槛：模型判定"这一帧在嚼"的最低概率（默认 0.5）。
                        数值越大越不容易误报，但可能漏掉轻微的咀嚼
  head_motion_threshold  头部移动百分比超过多少就暂停计数（越小越敏感）

采样频率在 `app/config.py::HAND_MOUTH_INTERVAL`；咀嚼概率模型（多肌肉逻辑回归）
在 `models/chewing_model.json`，重新训练见 `python -m app.vision.chewing_engine --calibrate`。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import PATHS

DEFAULTS: dict[str, float] = {
    "bite_dist_ratio": 1.5,
    "bite_leave_sec": 0.6,
    "bite_refractory_sec": 1.5,
    "chew_fast_threshold": 30.0,
    "chew_slow_threshold": 15.0,
    "chew_prob_threshold": 0.5,
    "head_motion_threshold": 4.0,
}

PATH = Path(PATHS.models_dir) / "eating_thresholds.json"


def load() -> dict[str, float]:
    """读参数文件；缺失时写出默认值，异常时退回默认值（不影响检测）。"""
    values = dict(DEFAULTS)
    raw: dict = {}
    try:
        if PATH.exists():
            # utf-8-sig：用记事本编辑保存常会带 BOM，普通 utf-8 会解析失败
            raw = json.loads(PATH.read_text(encoding="utf-8-sig"))
            for key, default in DEFAULTS.items():
                if key in raw:
                    values[key] = type(default)(raw[key])
        # 缺项补齐写回：保证文件里始终能看到全部可调参数
        if [k for k in DEFAULTS if k not in raw]:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    except Exception:
        pass
    return values
