"""FastAPI 应用工厂：API 路由 + 前端静态托管。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi import Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .api import agent, audio, device, dialogue, report, settings, vision

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app() -> FastAPI:
    app = FastAPI(title="正念饭崽 mindful_meal", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    app.include_router(vision.router)
    app.include_router(dialogue.router)
    app.include_router(audio.router)
    app.include_router(report.router)
    app.include_router(settings.router)
    app.include_router(device.router)
    app.include_router(agent.router)

    @app.get("/api/health")
    async def health():
        # tuning 只列出被 data_local/tuning.json（或环境变量）**覆盖过**的值，
        # 便于确认"改了到底生效没"，没改过就是空对象。
        return {"ok": True, "app": "mindful_meal", "version": __version__,
                "tuning": config.tuning_applied()}

    @app.on_event("startup")
    async def _warmup():
        """后台预热语音模型：首次识别/合成可省几秒模型加载时间。"""
        import sys
        import threading

        def _load():
            try:
                from .voice.asr import active_engine, warmup
                ok = warmup()
                print(f"[warmup] 识别引擎 {active_engine()} "
                      f"{'已就绪' if ok else '不可用（跳过）'}", flush=True)
            except Exception as exc:
                print(f"[warmup] 识别引擎跳过: {exc}", flush=True)
            # 语音唤醒：sherpa-onnx KWS（中文唤醒词专用模型）
            try:
                from .voice.sherpa_kws import kws_detector
                if kws_detector.available():
                    ok = kws_detector.warmup()
                    print(f"[warmup] 唤醒引擎 sherpa-kws "
                          f"{'已就绪' if ok else '加载失败：' + str(kws_detector.load_error())}",
                          flush=True)
                else:
                    print("[warmup] 唤醒模型缺失，语音唤醒不可用；"
                          "请运行 python tools/download_sherpa_models.py", flush=True)
            except Exception as exc:
                print(f"[warmup] 唤醒引擎跳过: {exc}", flush=True)
            # 发声：Qwen3-TTS-Flash（阿里云百炼，云端）。没有本地模型要加载，
            # 这里只做一次配置自检与一次真实合成探活，把结果打清楚，
            # 免得用户以为"没声音"是代码问题。
            try:
                import time as _time

                from .voice import qwen_tts_engine, tts as voice_tts
                st = qwen_tts_engine.status()
                if not st.get("configured"):
                    print("[warmup] 发声：Qwen3-TTS 未配置 API Key —— 语音合成不可用，"
                          "请在网页设置页填入 TTS API Key（阿里云百炼 DASHSCOPE_API_KEY）",
                          flush=True)
                else:
                    t0 = _time.time()
                    out = config.PATHS.media_dir / "audio" / "warmup-tts.wav"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    ok = voice_tts.synthesize("我在", out)
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    print(f"[warmup] 发声：{st.get('model')} 音色={st.get('voice')} "
                          f"{'就绪' if ok else '探测失败（首句会回落提示音）'}"
                          f"（{_time.time() - t0:.1f}s）", flush=True)
                    if not ok:
                        print(f"[warmup] 发声探测错误：{qwen_tts_engine.status().get('last_error')}",
                              flush=True)
            except Exception as exc:
                print(f"[warmup] 发声跳过: {exc}", flush=True)
            # 启动视觉感知（设备屏摄像头页/表情页指标都要用到；
            # 无摄像头时自动降级为 Mock 模拟画面，不会报错）
            try:
                from .api.agent import apply_settings
                from .core.interaction_config import get_settings
                applied = apply_settings()
                import time as _t
                _t.sleep(1.5)  # 等摄像头探测完成，日志里显示真实 mode
                from .vision.service import vision_service
                st = vision_service.status()
                mode = st.get("mode")
                # 启动瞬间相机可能还没就绪（掉到模拟画面），这里说清楚，免得看着像故障
                extra = ("—— 未探测到摄像头（先用模拟画面，每 10 秒会自动重试）"
                         if mode in ("mock", "error", None) else "")
                print(f"[vision] 视觉感知 {'已启动' if applied.get('vision') else '已关闭（日常聊天模式）'}"
                      f" mode={mode} fps={st.get('fps') or 0:.1f}{extra}", flush=True)
                print(f"[agent] 模式={applied.get('mode_name')} "
                      f"自主互动={applied.get('autonomy_name')}", flush=True)
            except Exception as exc:
                print(f"[vision] 视觉感知启动失败: {exc}", flush=True)
            # 自主互动：AI 不等用户开口，按情绪/进食数据主动说话
            try:
                from .core.autonomy import autonomy_service
                autonomy_service.start()
                print("[agent] 自主互动服务已启动", flush=True)
            except Exception as exc:
                print(f"[agent] 自主互动启动失败: {exc}", flush=True)
            # 定期清理缓存/日志/过期事件（长期无人值守跑的设备必备）
            try:
                from .core.janitor import cache_janitor
                cache_janitor.start()
                print(f"[janitor] 缓存清理已启动（每 {config.CACHE_CLEAN_INTERVAL_H:g} 小时："
                      f"媒体 {config.CACHE_MEDIA_KEEP_HOURS:g}h、日志 {config.CACHE_LOG_MAX_MB:g}MB、"
                      f"事件 {config.CACHE_EVENT_KEEP_DAYS:g} 天）", flush=True)
            except Exception as exc:
                print(f"[janitor] 缓存清理启动失败: {exc}", flush=True)
            # 只预热"必须秒回"的一句：唤醒应答「我在」。
            # （生成等待期不再有填充语，所以这里没有别的句子要预热。）
            try:
                # 测试环境跳过：预热会真的打云端接口合成一句，拖慢 TestClient 启动
                if "pytest" in sys.modules:
                    print("[warmup] 测试环境，跳过唤醒应答预热", flush=True)
                else:
                    from .core.interaction_config import WAKE
                    from .voice.tts import active_engine
                    from .voice.tts_cache import prewarm_ack, size
                    ok = prewarm_ack(str(WAKE.get("ack_text") or "").strip())
                    print(f"[warmup] 唤醒应答{'已预热' if ok else '预热失败'}（「{WAKE.get('ack_text')}」，"
                          f"当前音色 {active_engine()}，缓存共 {size()} 条）", flush=True)
            except Exception as exc:
                print(f"[warmup] 唤醒应答预热跳过: {exc}", flush=True)

        config.PATHS.ensure_dirs()
        applied_tuning = config.tuning_applied()
        if applied_tuning:
            print("[config] 已应用 data_local/tuning.json 的覆盖：" +
                  ", ".join(f"{k}={v}" for k, v in applied_tuning.items()), flush=True)
        # 全新安装时数据库还不存在：建一次表（`create table if not exists`，幂等），
        # 否则首次运行的健康分析/报告会因为"没有 events 表"而报错。
        try:
            from .core.db import HealthStore
            HealthStore().init()
        except Exception as exc:
            print(f"[db] 初始化数据库失败: {exc}", flush=True)
        threading.Thread(target=_load, name="voice-warmup", daemon=True).start()

        # 设备小屏：启动渲染线程（无硬件时只产出 PNG，供网页镜像查看）
        try:
            from .display.service import screen_service
            from .display.state import display_state
            st = screen_service.start()
            print(f"[screen] 设备屏已启动 {st['size']} @{config.SCREEN_FPS}fps 输出={st['output']}",
                  flush=True)
            if config.SCREEN_BUTTONS:
                threading.Thread(target=screen_service.start_buttons,
                                 name="screen-buttons", daemon=True).start()
            # 同步 AI 配置状态（设备屏据此决定是否显示"去设置页配置"提示）
            try:
                from .core.ai_config import AIConfigStore
                cfg = AIConfigStore().load()
                display_state.set_ai_configured(bool(cfg and cfg.api_url and cfg.key_set))
            except Exception:
                pass
        except Exception as exc:
            print(f"[screen] 设备屏启动失败: {exc}", flush=True)

    # 前端静态资源
    # 自研 JS/CSS 一律不缓存：设备屏长期跑在 kiosk 浏览器里，若浏览器还拿旧的
    # device_ui.js，表现就是"改了代码没生效"甚至"卡在正在连接设备屏…"，极难排查
    # （本项目真踩过：index.html 的 ?v= 版本号没跟着改，浏览器一直用旧脚本）。
    # 注意：这里刻意不用 BaseHTTPMiddleware —— 它会干扰 SSE/MJPEG 流式响应。
    class NoStoreStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            # 注意：Windows 上传进来的 path 用反斜杠（'js\\device_ui.js'），
            # 必须先归一化再判断，否则 startswith("js/") 永远为假、头设不上。
            normalized = str(path).replace("\\", "/")
            if normalized.startswith(("js/", "css/")):
                response.headers["Cache-Control"] = "no-store, max-age=0"
            return response

    app.mount("/static", NoStoreStaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # 页面 HTML 也不缓存：否则服务没起来时浏览器会拿缓存的旧页面渲染，
    # 用户看到的是"卡在正在连接设备屏…"而不是"连不上服务"，完全误导排查方向。
    _NO_STORE = {"Cache-Control": "no-store, max-age=0"}

    @app.get("/", include_in_schema=False)
    async def index():
        """默认前端 = 设备屏仿真（与硬件屏像素级一致，可左右滑动切页）。"""
        return FileResponse(WEB_DIR / "static" / "index.html", headers=_NO_STORE)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        icon = WEB_DIR / "static" / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)

    return app


app = create_app()


def run_server(host: str = "0.0.0.0", port: int = 8765) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
