"""检查前端缓存头是否按预期设置（HTML 与自研 JS/CSS 都应为 no-store）。"""
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8765"
PATHS = ["/",
         "/static/js/device_ui.js?v=20260915b",
         "/static/css/device_ui.css?v=20260915b",
         "/static/vendor/agent-robot-avatar/agent-robot-avatar.js"]

for path in PATHS:
    try:
        with urllib.request.urlopen(BASE + path, timeout=8) as r:
            cc = r.headers.get("Cache-Control") or "(未设置 → 可被缓存)"
        print(f"  {path:56s} → {cc}")
    except Exception as exc:
        print(f"  {path:56s} → 请求失败 {exc}")
