"""桌面版入口（PyInstaller 打包成 exe 时用它当入口）。

它做的事：
  1. 找一个可用端口（默认 8765，被占用就往后顺延）；
  2. 在后台线程里启动 web 服务（就是 `python -m app.main serve` 那套）；
  3. 用默认浏览器打开设备屏页面（`--kiosk` 时用 Edge/Chrome 的应用模式，像一块真屏）；
  4. 控制台打印日志与"关闭窗口即退出"的提示，Ctrl+C 干净退出。

数据和模型都放在 **exe 旁边**（见 app/config.py::app_root）：`data_local/` 与 `models/`。
模型不打进 exe：14 万参数的 KWS + 78MB Paraformer 每次启动都解压一遍会很慢
（`--onedir` 与 onefile 的区别就在这里）。exe 首次启动会在自己旁边生成 `data_local/`。
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _root() -> Path:
    """exe 旁边的目录（源码运行时是仓库根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as s:          # 都不行就让系统给一个
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_browser(url: str, kiosk: bool) -> None:
    if kiosk:
        import subprocess
        for exe, args in (
            ("msedge", ["--app=" + url, "--start-maximized"]),
            ("chrome", ["--app=" + url, "--start-maximized"]),
        ):
            try:
                subprocess.Popen([exe, *args], shell=False)
                return
            except Exception:
                continue
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="正念饭崽 桌面版")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="只起服务，不打开浏览器")
    ap.add_argument("--kiosk", action="store_true", help="用浏览器应用模式打开（更像一块设备屏）")
    args = ap.parse_args()

    root = _root()
    (root / "data_local").mkdir(parents=True, exist_ok=True)
    if not (root / "models").is_dir():
        print(f"[提示] 没找到模型目录：{root / 'models'}\n"
              f"       唤醒与识别需要它（约 140MB），请把发行包里的 models/ 放在 exe 旁边。",
              flush=True)
    if not (root / "data_local" / "ai_config.json").is_file():
        print(f"[提示] 还没有 {root / 'data_local' / 'ai_config.json'}"
              f"（可复制 ai_config.json.template 并填 Key）—— 现在只出文字，没有说话与语音。",
              flush=True)

    port = _free_port(args.port)
    url = f"http://{args.host}:{port}/"

    import uvicorn

    from app.server import create_app

    app = create_app()
    config = uvicorn.Config(app, host=args.host, port=port, log_level="info")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="desktop-server", daemon=True)
    thread.start()
    for _ in range(100):                      # 等端口起来再开浏览器
        if getattr(server, "started", False):
            break
        time.sleep(0.1)

    print("=" * 56, flush=True)
    print(f"  正念饭崽 已启动：{url}", flush=True)
    print(f"  数据目录：{root / 'data_local'}", flush=True)
    print("  关闭这个窗口（或按 Ctrl+C）即退出程序", flush=True)
    print("=" * 56, flush=True)
    if not args.no_browser:
        _open_browser(url, args.kiosk)

    try:
        while thread.is_alive():
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n[退出] 正在停止服务…", flush=True)
        server.should_exit = True
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
