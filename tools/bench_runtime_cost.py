"""运行期性能体检：**这个服务到底在烧多少 CPU、一轮对话要多久**。

为什么要这个工具：用户报"卡顿/反应慢"时，光看服务在跑是分不出"哪一段慢"的。
这里把两件事都量出来，并在浏览器停在每一页时分别采样：

  1. CPU：空闲时 / 浏览器停在表情页 / 停在摄像头页（MJPEG）/ 停在报告页
     —— 单纯"服务在跑"应该是 0.2~0.4 核；如果连上浏览器就飙到 1.5+ 核，
       说明是渲染/流/唤醒这几条链路里的某一条在空烧（详见 README「性能」一节）。
  2. 对话延迟：首句文本 / 首段语音 / 整轮结束（走真实 /api/dialogue/stream）。

用法（服务已在 8765 跑着）：
    .venv/Scripts/python tools/bench_runtime_cost.py                # 用 8765
    .venv/Scripts/python tools/bench_runtime_cost.py --port 8766    # 量另一个实例
    .venv/Scripts/python tools/bench_runtime_cost.py --pid 1234     # 指定进程采样 CPU
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _ps_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                        "WindowsPowerShell", "v1.0", "powershell.exe")


def cpu_seconds(pid: int) -> float:
    out = subprocess.run([_ps_exe(), "-NoProfile", "-Command",
                          f"(Get-Process -Id {pid}).CPU"],
                         capture_output=True, text=True)
    return float(out.stdout.strip().replace(",", "."))


def find_pid(port: int) -> int:
    """找到监听该端口的进程（先问系统谁在 listen，再退回按命令行匹配）。"""
    out = subprocess.run([_ps_exe(), "-NoProfile", "-Command",
                          f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                          "-ErrorAction SilentlyContinue | Select-Object -First 1 "
                          "-ExpandProperty OwningProcess)"],
                         capture_output=True, text=True)
    if out.stdout.strip().isdigit():
        return int(out.stdout.strip())
    out = subprocess.run([_ps_exe(), "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                         capture_output=True, text=True)
    try:
        data = json.loads(out.stdout or "[]")
    except Exception:
        data = []
    if isinstance(data, dict):
        data = [data]
    for row in data:
        cmd = str(row.get("CommandLine") or "")
        if "app.main" in cmd and str(port) in cmd:
            return int(row["ProcessId"])
    return 0


def sample(label: str, pid: int, seconds: float) -> float:
    c0, t0 = cpu_seconds(pid), time.time()
    time.sleep(seconds)
    c1, dt = cpu_seconds(pid), time.time() - t0
    cores = (c1 - c0) / dt
    print(f"  {label:28s} {cores:5.2f} 核", flush=True)
    return cores


def dialogue_latency(base: str, message: str, user_id: str) -> dict:
    t0, first_text, first_audio, done = time.time(), None, None, None
    text, audio = [], 0
    with requests.post(f"{base}/api/dialogue/stream",
                       json={"message": message, "user_id": user_id},
                       stream=True, timeout=300) as r:
        ev = None
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                d = json.loads(line[5:].strip())
                if ev == "reply":
                    first_text = first_text or time.time() - t0
                    text.append(d.get("text", ""))
                elif ev == "audio":
                    first_audio = first_audio or time.time() - t0
                    audio += 1 if d.get("audio_b64") else 0
                elif ev == "done":
                    done = time.time() - t0
    return {"first_text": first_text, "first_audio": first_audio, "done": done,
            "audio_chunks": audio, "reply": "".join(text)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pid", type=int, default=0, help="不填则按端口自动找")
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    pid = args.pid or find_pid(args.port)
    print(f"服务 {base} | 进程 PID {pid or '未知'}")

    if pid:
        print("\n=== CPU（每档 12 秒）===")
        sample("空闲（没有浏览器）", pid, 12)
        if not args.skip_browser:
            try:
                from playwright.sync_api import sync_playwright
            except Exception:
                sync_playwright = None
            if sync_playwright:
                with sync_playwright() as p:
                    b = p.chromium.launch(headless=True, channel="msedge",
                                          args=["--no-sandbox",
                                                "--use-fake-device-for-media-stream",
                                                "--use-fake-ui-for-media-stream"])
                    pg = b.new_page(viewport={"width": 900, "height": 760})
                    pg.goto(base, wait_until="domcontentloaded")
                    pg.wait_for_function("() => !!window.__dshBooted", timeout=30000)
                    sample("浏览器：表情页", pid, 12)
                    pg.evaluate("() => goPage('camera')")
                    pg.wait_for_function("() => _page === 'camera'")
                    sample("浏览器：摄像头页(MJPEG)", pid, 14)
                    pg.evaluate("() => goPage('stats')")
                    pg.wait_for_function("() => _page === 'stats'")
                    sample("浏览器：健康报告页", pid, 12)
                    b.close()
                    time.sleep(3)
                    sample("浏览器已关闭", pid, 12)
            else:
                print("  （没装 playwright，跳过浏览器分档）")

    print("\n=== 一轮对话延迟 ===")
    r = dialogue_latency(base, "我有点饿", "perf-bench")
    def fmt(v):
        return f"{v:.1f}s" if v else "—"
    print(f"  首句文本 {fmt(r['first_text'])} | 首段语音 {fmt(r['first_audio'])} | "
          f"整轮 {fmt(r['done'])} | 语音块 {r['audio_chunks']}")
    print(f"  回复: {r['reply'][:80]}")
    print("\n判读：首句 >8s 一般说明模型还在「思考模式」（ai_config.json 里 "
          "enable_thinking 应为 false，且改完要重启服务）；连上浏览器就 >1.5 核，"
          "见 README 的性能一节。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
