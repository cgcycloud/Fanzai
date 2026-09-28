"""把项目打包成**单文件桌面程序**（Windows .exe，PyInstaller）。

产物：`dist/exe/mindful-meal.exe`（一个文件，双击即用）+ `dist/exe/models/`（模型，放 exe 旁边）。

为什么模型不塞进 exe：
  PyInstaller 的 onefile 每次启动都会把内嵌内容解压到 `%TEMP%\\_MEIxxxx` ——
  140MB 的模型意味着**每次启动多等十几秒**、还会反复写磁盘。所以 exe 只装代码与依赖，
  模型解压一次放在 exe 旁边（`models/`），`data_local/` 同样在 exe 旁边生成。

用法：
    .venv/Scripts/python.exe -m pip install pyinstaller      # 只需一次
    .venv/Scripts/python.exe tools/build_exe.py             # 产出 dist/exe/
    .venv/Scripts/python.exe tools/build_exe.py --onedir    # 换成"文件夹版"（启动更快）
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]

# 这些包里有大量 C 扩展 / 数据文件，PyInstaller 的自动分析经常漏，显式收全
COLLECT_ALL = ("sherpa_onnx", "onnxruntime", "mediapipe", "cv2", "PIL", "cryptography")
# uvicorn 的协议实现是运行时按字符串加载的，静态分析看不到
HIDDEN_IMPORTS = ("uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                  "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
                  "app.server", "app.main")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true", help="打包成文件夹（启动更快，不是单文件）")
    ap.add_argument("--keep-temp", action="store_true", help="保留 build/ 中间产物")
    ap.add_argument("--with-config", action="store_true",
                    help="把本机 data_local/ai_config.json（含 API Key）复制到程序旁边，"
                         "省得重新填。**自己用才加这个参数**：带了 Key 的包不要外发")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("缺少 pyinstaller，请先安装：\n  .venv/Scripts/python -m pip install pyinstaller")
        return 1

    dist = ROOT / "dist" / "exe"
    build = ROOT / "build" / "exe"
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "mindful-meal",
        "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(build), "--specpath", str(build),
        "--onedir" if args.onedir else "--onefile",
        # 设备屏前端（HTML/CSS/JS/表情组件）必须一起打进去
        "--add-data", f"{ROOT / 'app' / 'web' / 'static'}{';' if sys.platform == 'win32' else ':'}app/web/static",
        "--console",                      # 保留控制台：用户能看到日志与报错
    ]
    for pkg in COLLECT_ALL:
        cmd += ["--collect-all", pkg]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    cmd.append(str(ROOT / "tools" / "desktop_app.py"))

    print("正在打包（首次约 2~6 分钟，体积 200MB+）…", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"❌ 打包失败（返回码 {r.returncode}），上面是 PyInstaller 的日志")
        return r.returncode

    # 模型放在 exe 旁边（按发行包同样的裁剪规则：KWS 只留运行时那四个文件）
    src_models = ROOT / "models"
    dst_models = dist / "models"
    kws = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    keep = {"tokens.txt", "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"}
    for p in sorted(src_models.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src_models)
        if len(rel.parts) >= 2 and rel.parts[0] == kws and rel.name not in keep:
            continue
        out = dst_models / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)

    (dist / "data_local").mkdir(exist_ok=True)
    tpl = ROOT / "data_local" / "ai_config.json.template"
    if tpl.is_file():
        shutil.copy2(tpl, dist / "data_local" / "ai_config.json.template")
    readme = ROOT / "data_local" / "AI_CONFIG.md"
    if readme.is_file():
        shutil.copy2(readme, dist / "data_local" / "AI_CONFIG.md")
    tuning_example = ROOT / "data_local" / "tuning.json.example"
    if tuning_example.is_file():
        shutil.copy2(tuning_example, dist / "data_local" / "tuning.json.example")
    copied_config = False
    if args.with_config:
        for name in ("ai_config.json", "ai_config.key"):
            src = ROOT / "data_local" / name
            if src.is_file():
                shutil.copy2(src, dist / "data_local" / name)
                copied_config = copied_config or name == "ai_config.json"

    exe = dist / ("mindful-meal.exe" if sys.platform == "win32" else "mindful-meal")
    size = exe.stat().st_size / 1024 / 1024 if exe.exists() else 0
    print(f"\n✅ 完成（{time.time() - t0:.0f}s）")
    print(f"   程序：{exe}  {size:.0f} MB")
    print(f"   模型：{dst_models}（放在程序旁边，缺了就没有唤醒/识别）")
    print("   双击运行即可；参数：--port 8765 --kiosk --no-browser")
    if copied_config:
        print("   已附带本机 ai_config.json（含 Key）—— 这份目录请勿外发")
    if not args.keep_temp:
        shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
