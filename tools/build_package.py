"""打一份干净的发行包（生产代码 + 模型 + 部署脚本），开发目录原样保留。

为什么不直接压缩整个项目：开发目录里有 `.venv`（683MB）、`tests/`、`tools/` 诊断脚本、
日志、截图、旧备份、以及**你自己的 API Key 与对话记录** —— 这些都不该进发行包。

用法（在项目根目录执行）：

    .venv/Scripts/python.exe tools/build_package.py                # 打到 dist/mindful-meal-<版本>/
    .venv/Scripts/python.exe tools/build_package.py --out D:\\pkg   # 换个输出目录
    .venv/Scripts/python.exe tools/build_package.py --zip           # 顺手压成 zip

包里有什么、没什么，都在下面的清单里写死（改这里即可，不用手抄目录）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import __version__  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# ---- 要打包的目录（整目录递归，按下面的黑名单过滤）----
INCLUDE_DIRS = ("app", "deploy", "docs")
# ---- 要打包的散件 ----
INCLUDE_FILES = ("requirements.txt", "requirements-pi.txt", "pyproject.toml", "run.bat")

# ---- 一律不要的东西（开发/调试/个人数据）----
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".venv", "tests", "tools",
                "gui-test-screenshots", "dist", ".idea", ".vscode"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".bak", ".orig", ".tmp"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", "ai_config.key", "user_health.db",
                 "live_snapshot.json", "model_probe.json", "wake_debug.pcm",
                 "_server.log", "_server.err", "wake_keywords.txt"}
EXCLUDE_NAME_PREFIXES = ("conversation_memory_", "wake-ack-", "prewarm-",
                         "tts-", "proactive-", "ack-", "warmup-")

# ---- 模型：只留运行时真正读到的文件（KWS 只认 epoch-12 的 int8 三件套 + tokens）----
KWS_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_KEEP = {
    "tokens.txt",
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
}

# 发行包里 data_local 只放"说明 + 配置模板"，不带 Key、不带历史数据
DATA_FILES = ("AI_CONFIG.md", "tuning.json.example")
TEMPLATE_CONFIG = """{
  "api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "model": "qwen3.7-flash",
  "api_key": "在这里填对话用的 Key（百炼 DashScope）",
  "enable_thinking": false,
  "vision_model": "glm-4v-flash",
  "vision_api_url": "https://open.bigmodel.cn/api/paas/v4",
  "vision_api_key": "在这里填视觉用的 Key（智谱，可留空）",
  "tts_api_url": "https://dashscope.aliyuncs.com/api/v1",
  "tts_api_key": "在这里填发声用的 Key（百炼，可与 api_key 同一把）",
  "tts_model": "qwen3-tts-flash",
  "tts_voice": "Cherry",
  "tts_language": "Chinese",
  "tts_format": "wav"
}
"""


def _skip(name: str, rel: str) -> bool:
    if name in EXCLUDE_NAMES:
        return True
    # 下划线开头的是运行期临时产物（_server.log / _speech_only.wav），
    # 但 **dunder 文件（__init__.py）必须保留** —— 曾经因为"下划线开头"一刀切，
    # 把 app/__init__.py 也排除了，包里的服务直接 ImportError 起不来。
    stem = Path(name).stem
    if name.startswith("_") and not (stem.startswith("__") and stem.endswith("__")):
        return True
    if any(name.startswith(p) for p in EXCLUDE_NAME_PREFIXES):
        return True
    if Path(name).suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    # models/ 下按白名单过滤**所有**文件（KWS 目录里的示例音频/README/configuration.json
    # 运行时都不读：唤醒词表是启动时生成到 data_local/wake_keywords.txt 的）
    return rel.startswith("models/") and not _model_wanted(rel)


def _model_wanted(rel: str) -> bool:
    """models/ 下的白名单：KWS 目录只留运行时读到的四个文件；其余模型文件都要。"""
    parts = Path(rel).parts
    if len(parts) >= 2 and parts[1] == KWS_DIR:
        return len(parts) == 3 and parts[2] in KWS_KEEP
    return True


def _copy_file(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return src.stat().st_size


def build(out_root: Path, *, make_zip: bool = False) -> tuple[Path, dict]:
    pkg = out_root / f"mindful-meal-{__version__}"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    stat = {"files": 0, "bytes": 0, "skipped": []}

    def add(src: Path, rel_to: Path | None = None) -> None:
        rel = str((rel_to or src).as_posix())
        if _skip(src.name, rel):
            stat["skipped"].append(rel)
            return
        stat["bytes"] += _copy_file(src, pkg / rel)
        stat["files"] += 1

    for d in INCLUDE_DIRS:
        src_dir = ROOT / d
        if not src_dir.is_dir():
            continue
        for p in sorted(src_dir.rglob("*")):
            rel = p.relative_to(ROOT)
            if p.is_dir():
                if p.name in EXCLUDE_DIRS:
                    continue
                continue
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            add(p, rel)

    for f in INCLUDE_FILES:
        if (ROOT / f).is_file():
            add(ROOT / f, Path(f))

    # 模型（裁剪后）
    for p in sorted((ROOT / "models").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        add(p, rel)

    # data_local：说明 + 配置模板（不带任何个人数据）
    for name in DATA_FILES:
        if (ROOT / "data_local" / name).is_file():
            add(ROOT / "data_local" / name, Path("data_local") / name)
    tpl = pkg / "data_local" / "ai_config.json.template"
    tpl.parent.mkdir(parents=True, exist_ok=True)
    tpl.write_text(TEMPLATE_CONFIG, encoding="utf-8")

    (pkg / "README.md").write_text(_readme(), encoding="utf-8")
    (pkg / "start.sh").write_text(_start_sh(), encoding="utf-8")
    (pkg / "VERSION").write_text(f"{__version__}\n", encoding="utf-8")

    if make_zip:
        zip_path = out_root / f"{pkg.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(pkg.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(out_root))
        stat["zip"] = str(zip_path)
    return pkg, stat


def _readme() -> str:
    return f"""# 正念饭崽（mindful-meal {__version__}）发行包

面向慢病人群的 AI 正念饮食陪伴助手：树莓派 + 2.8 寸小屏 + 摄像头 + 麦克风，
通过"餐前—餐中—餐后"的正念引导帮助用户慢下来、觉察饥饿与饱足。

本包是**可直接运行的生产发行版**（不含测试与调试脚本）。

## 一、准备

1. **Python 3.10+**（Windows 上安装时勾选 "Add Python to PATH"）。
2. **填 AI 配置**：把 `data_local/ai_config.json.template` 复制成
   `data_local/ai_config.json`，按里面的注释填入 API Key（详见 `data_local/AI_CONFIG.md`）。
   不填也能跑，但只能出文字、没有真实对话与语音。
3. **本地模型随包附带**（`models/`，约 140MB）：唤醒 = sherpa-onnx KWS、
   识别 = Paraformer，**完全离线、不联网**；只有"对话 / 视觉 / 发声"三项走云端。

## 二、运行

| 平台 | 命令 |
|---|---|
| Windows | 双击 `run.bat`（首次会自动建虚拟环境并装依赖） |
| Linux / 树莓派 | `chmod +x start.sh && ./start.sh`（树莓派一键装机见 `deploy/setup_pi.sh`） |

启动后打开 **http://127.0.0.1:8765/** —— 这整屏就是设备屏：

* **说「你好饭崽」** → 应答一声「我在」→ 说话（自动断句）→ AI 回复并播报；
* **轻触屏幕** → 手动开始说话；**左右滑动** → 切页（表情 / 摄像头 / 健康报告）；
* **在表情页下滑** → 设置面板：亮度、音量、AI 语速、语音播报开关、运行模式、
  自主互动级别、语音唤醒（开关 + 单句/连续）、时间校准。

## 三、常用命令

```bash
python -m app.main serve                 # 启动服务（默认 0.0.0.0:8765）
python -m app.main self-test             # 硬件自检（摄像头/音频/GPIO）
python -m app.main init-db               # 初始化数据库（首次运行会自动做）
python -m app.main analytics             # 完整健康分析 JSON
python -m app.main weekly-review         # 近 7 天周报 JSON
python -m app.main cleanup               # 清理缓存（媒体临时文件/日志/过期事件）
python -m app.main tts "文本" --play      # 试听发声
python -m app.main transcribe a.wav      # 本地识别一段 WAV
python -m app.main ai-config --help      # 写 AI 配置（也可直接改 json 文件）
```

## 三之补充、想调数值（上下文长度 / 帧率等）

不用改代码，也不用重新打包：把 `data_local/tuning.json.example` 另存为
`data_local/tuning.json`，改里面的数值，**重启服务生效**（改完启动日志会打印
`[config] 已应用 … 的覆盖`，`GET /api/health` 的 `tuning` 字段也会列出来）。

| 键 | 默认 | 说明 |
|---|---|---|
| `memory_send_tokens` | 2000 | **每次请求真正发给模型的上文**（最常调的就是它）：调大更"记得住"，但首句更慢 |
| `memory_token_limit` | 128000 | 上下文窗口硬上限 |
| `memory_reserve_tokens` / `memory_keep_recent_turns` | 8000 / 8 | 摘要压缩的预留与保留轮数 |
| `screen_camera_fps` | 30 | 摄像头页帧率（树莓派上卡就调 15） |
| `screen_idle_fps` | 2.5 | 没人看屏幕时的兜底帧率 |
| `kws_num_threads` | 1 | 唤醒解码线程数（**保持 1 最省 CPU**） |

## 四、目录

```
app/           后端 + 设备屏前端（web/static 里是页面与表情组件）
models/        离线模型（KWS 唤醒 / Paraformer 识别 / 人脸·手部·情绪）
data_local/    运行数据与配置（首次启动自动生成；ai_config.json 要自己填 Key）
deploy/        树莓派部署（systemd 服务 + 安装脚本）
docs/          产品说明
run.bat         Windows 一键启动
start.sh        Linux / 树莓派一键启动
```

## 五、说明

* **语音链路**：唤醒与识别**本地离线**（sherpa-onnx），**发声走云端**
  （阿里云百炼 Qwen3-TTS-Flash，需联网 + Key；没配 Key 时降级为本地提示音）。
* **隐私**：对话记录、用餐记录、情绪采样都只存在本机 `data_local/`；
  只有"对话/视觉/发声"三类请求会发到你配置的云服务。
* **缓存清理**：服务在后台每 6 小时清一次媒体临时文件、轮转日志、删除过期的高频事件
  （对话与用餐记录不会自动删）。
* **第三方**：表情组件 [agent-robot-avatar](https://github.com/CX-ArtLab/agent-robot-avatar)（MIT，已内置）、
  [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（Apache-2.0）、
  MediaPipe / ONNX Runtime / HSEmotion 等按其各自许可使用。
"""


def _start_sh() -> str:
    return """#!/usr/bin/env bash
# 正念饭崽 一键启动（Linux / 树莓派）。首次运行会建虚拟环境并装依赖。
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[初始化] 创建虚拟环境并安装依赖（首次约 3~10 分钟）..."
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

if [ ! -f data_local/ai_config.json ] && [ -f data_local/ai_config.json.template ]; then
  echo "[提示] 还没有 data_local/ai_config.json —— 现在只能出文字，"
  echo "       想用真实对话/语音请复制 ai_config.json.template 并填入 Key（见 data_local/AI_CONFIG.md）"
fi

PORT="${PORT:-8765}"
echo "[启动] http://127.0.0.1:${PORT}/   （Ctrl+C 停止）"
exec ./.venv/bin/python -m app.main serve --port "${PORT}"
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--zip", action="store_true", help="同时打成 zip")
    args = ap.parse_args()
    t0 = time.time()
    pkg, stat = build(Path(args.out), make_zip=args.zip)
    print(f"✅ 发行包已生成：{pkg}")
    print(f"   {stat['files']} 个文件 · {stat['bytes'] / 1024 / 1024:.1f} MB · "
          f"{time.time() - t0:.1f}s")
    print("   已排除：.venv / tests / tools / 日志 / 截图 / 你的 Key 与对话记录 / "
          "KWS 里用不到的 epoch-99 与非量化权重")
    if stat.get("zip"):
        print(f"   压缩包：{stat['zip']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
