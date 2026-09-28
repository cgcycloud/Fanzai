# 等服务就绪后再打开浏览器（由 run.bat 调用）。
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
