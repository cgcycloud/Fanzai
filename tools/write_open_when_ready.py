"""用 utf-8-sig(BOM) 写入 open_when_ready.ps1。

Windows PowerShell 5.1 读取**无 BOM** 的 .ps1 会按 ANSI 解码，
中文注释被破坏后会直接解析失败（实测报 "Unexpected token ')'"）。
编辑器/脚本工具常把 BOM 丢掉，所以这里统一由本脚本落地。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONTENT = '''# 等服务就绪后再打开浏览器（由 run.bat 调用）。
#
# 为什么不用固定延时：冷启动要加载 ASR(82MB)/KWS 本地模型，6 秒未必就绪；
# 浏览器先打开会连不上，若再叠加页面缓存，用户就会看到"一直卡在 正在连接设备屏…"
# 且毫无提示。这里轮询 /api/health，真正就绪才开浏览器。
#
# 注意：本文件必须以 UTF-8 with BOM 保存，否则 Windows PowerShell 5.1 会按 ANSI
# 解码中文并解析失败。改动请用 tools/write_open_when_ready.py 重新生成。
param(
    [int]$Port = 8765,
    [int]$TimeoutSec = 120,
    [switch]$DryRun          # 只探测不开浏览器（自测用）
)

$page = "http://127.0.0.1:$Port/"
$url = "http://127.0.0.1:$Port/api/health"
for ($i = 0; $i -lt $TimeoutSec; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $url
        if ($r.StatusCode -eq 200) {
            Write-Host "[服务就绪] 用时约 $i 秒，正在打开 $page"
            if (-not $DryRun) { Start-Process $page }
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 1000
}
Write-Host "[提示] 服务 $TimeoutSec 秒内未就绪：请查看启动窗口的报错。"
Write-Host "       常见原因：端口 $Port 被占用（上次的服务没关）、依赖或模型缺失。"
exit 1
'''

out = Path(__file__).resolve().parent / "open_when_ready.ps1"
out.write_text(CONTENT, encoding="utf-8-sig")
raw = out.read_bytes()[:3]
print(f"已写入 {out.name}，BOM={raw == b'\\xef\\xbb\\xbf'}（{raw!r}）")
