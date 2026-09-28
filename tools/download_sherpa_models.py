"""下载 sherpa-onnx 的中文 KWS（关键词唤醒）模型。

GitHub release 直连在国内常超时，这里走可用代理（ghfast.top / gh-proxy.com / ghproxy.net），
并支持断点重试。解压到 models/ 下，与既有模型目录并列。

注意：本地 TTS 音色模型（Kokoro / Matcha / VITS / Piper）已全部移除，发声改为云端
Qwen3-TTS-Flash，所以本脚本**只下载唤醒模型**，不再下载任何 TTS 模型。
识别用的 Paraformer 走 tools/download_models.py。
"""
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
UA = {"User-Agent": "Mozilla/5.0"}

PROXIES = ["https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/", ""]

TARGETS = [
    ("kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2",
     "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"),
]
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"


def fetch(rel: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"已存在，跳过下载：{dest.name} ({dest.stat().st_size / 1048576:.1f}MB)")
        return True
    for proxy in PROXIES:
        url = proxy + BASE + rel
        label = proxy or "直连"
        try:
            print(f"下载 {rel}\n     通过 {label} …", flush=True)
            t0 = time.time()
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total and got % (4 * 1048576) < 262144:
                        pct = got * 100 / total
                        print(f"       {got / 1048576:5.1f}/{total / 1048576:.1f}MB "
                              f"({pct:.0f}%)", flush=True)
            print(f"      完成 {got / 1048576:.1f}MB，用时 {time.time() - t0:.0f}s")
            # 大小校验：代理偶发中断会留下截断的包，解压出来的模型是坏的
            if total and got < total:
                raise RuntimeError(f"下载不完整 {got}/{total} 字节")
            return True
        except Exception as exc:
            print(f"      失败（{label}）：{type(exc).__name__} {str(exc)[:70]}")
            if dest.exists():
                dest.unlink()
    return False


MODELS.mkdir(parents=True, exist_ok=True)

for rel, folder in TARGETS:
    out_dir = MODELS / folder
    if out_dir.exists() and any(out_dir.iterdir()):
        print(f"目标目录已存在：{out_dir.name}")
        continue
    tmp = MODELS / Path(rel).name
    if not fetch(rel, tmp):
        print(f"!! {rel} 全部源失败")
        continue
    print(f"解压 {tmp.name} → models/", flush=True)
    with tarfile.open(tmp, "r:bz2") as tar:
        tar.extractall(MODELS)
    tmp.unlink(missing_ok=True)
    if out_dir.exists():
        files = sorted(p.name for p in out_dir.iterdir())
        print(f"   {out_dir.name}/ → {files[:12]}")

print("\n完成。")
