"""正念饭崽 CLI 入口。

核心子命令：
  serve           启动 Web 服务（FastAPI + 前端，默认 0.0.0.0:8765）
  self-test       硬件自检（摄像头/音频/GPIO）
  init-db         初始化 SQLite 表
  ai-config       配置 AI API（Key 加密落盘）
  ai-chat         单条对话测试
  tts             文本转语音（可播放）
  transcribe      WAV → 文本（sherpa-onnx Paraformer）
  weekly-review   健康周报 JSON
  analytics       完整健康分析 JSON
  wake-listen     树莓派离线唤醒词监听
  assistant       树莓派语音助手循环（唤醒 → 录音 → ASR → AI → TTS 播报）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 注意：**不要**在这里把 stdout 重设成 cp936。
# Windows 控制台的底层缓冲（_WindowsConsoleIO）要求 UTF-8 字节，再自行转 UTF-16 输出；
# 一旦把 TextIOWrapper 改成 cp936，写出去的就是 GBK 字节，控制台按 UTF-8 解 → 中文全是乱码
# （日志重定向到文件时也一样，读出来是"ʶ������"这种）。之前正是这个改动导致后台终端乱码。
# 正确做法：由 run.bat 统一设 chcp 65001 + PYTHONUTF8=1，让两端都是 UTF-8。

from . import __version__
from . import config
from .core.db import HealthStore, utc_now


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mindful_meal", description="正念饭崽 —— 正念饮食慢病干预助手")
    p.add_argument("--version", action="version", version=f"mindful_meal {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 服务（对话/摄像头/看板/设置）")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8765)

    sub.add_parser("init-db", help="初始化数据库表")
    sub.add_parser("self-test", help="硬件自检")

    ai_config = sub.add_parser("ai-config", help="配置 AI API（明文写入 ai_config.json，可手改）")
    ai_config.add_argument("--api-url", required=True)
    ai_config.add_argument("--model", required=True)
    ai_config.add_argument("--api-key", help="对话用的 Key；留空保留原 Key")
    ai_config.add_argument("--system-prompt")
    ai_config.add_argument("--vision-model", help="视觉模型；留空保留原值")
    ai_config.add_argument("--vision-api-url", help="视觉单独用别的厂商时才填；留空沿用对话的")
    ai_config.add_argument("--vision-api-key", help="视觉单独用别的 Key 时才填；留空沿用对话的")

    ai_chat = sub.add_parser("ai-chat", help="发送一条 AI 对话")
    ai_chat.add_argument("message")

    tts = sub.add_parser("tts", help="文本转语音并保存")
    tts.add_argument("text")
    tts.add_argument("--output", type=Path)
    tts.add_argument("--play", action="store_true")

    transcribe = sub.add_parser("transcribe", help="本地识别 WAV（Paraformer）")
    transcribe.add_argument("path", type=Path)

    weekly = sub.add_parser("weekly-review", help="健康周报 JSON")
    weekly.add_argument("--user-id", default="default")
    weekly.add_argument("--days", type=int, default=7)

    sub.add_parser("analytics", help="完整健康分析（周报/风险/复查/洞察）")

    wake_test = sub.add_parser("wake-test", help="用 WAV 离线验证唤醒词是否命中（KWS）")
    wake_test.add_argument("path", type=Path)

    cleanup = sub.add_parser("cleanup", help="清理缓存：媒体临时文件 / 日志轮转 / 过期事件")
    cleanup.add_argument("--no-vacuum", action="store_true", help="不顺手 VACUUM 数据库")

    return p


def cmd_serve(args) -> None:
    from .server import run_server
    config.PATHS.ensure_dirs()
    print(f"正念饭崽 Web 服务: http://{args.host}:{args.port}  (数据目录 {config.PATHS.data_dir})")
    run_server(args.host, args.port)


def cmd_init_db(_) -> None:
    store = HealthStore()
    store.init()
    print_json({"ok": True, "db": str(config.PATHS.db_path)})


def cmd_self_test(_) -> None:
    from .hardware import detect_hardware
    print_json(detect_hardware())


def cmd_ai_config(args) -> None:
    from .core.ai_config import AIConfigStore
    c = AIConfigStore().save(
        api_url=args.api_url, model=args.model, api_key=args.api_key,
        system_prompt=args.system_prompt, vision_model=args.vision_model,
        vision_api_url=args.vision_api_url, vision_api_key=args.vision_api_key)
    print_json({"ok": True, "api_url": c.api_url, "model": c.model, "key_set": c.key_set})


def cmd_ai_chat(args) -> None:
    from .core.ai_client import get_ai_client
    reply = get_ai_client().chat(args.message)
    print_json({"ok": True, "reply": reply})


def cmd_tts(args) -> None:
    from .voice.tts import synthesize
    # Qwen3-TTS 返回 WAV
    out = args.output or (config.PATHS.media_dir / "audio" / "cli_tts.wav")
    ok = synthesize(args.text, out)
    print_json({"ok": ok, "path": str(out)})
    if ok and args.play:
        from .hardware import AudioIO
        AudioIO().play(out)


def cmd_cleanup(args) -> None:
    """手动跑一轮缓存清理（媒体临时文件 / 日志轮转 / 过期高频事件）。"""
    from .core.janitor import cleanup_once
    print_json({"ok": True, **cleanup_once(vacuum=not args.no_vacuum)})


def cmd_transcribe(args) -> None:
    from .voice.asr import transcribe_wav
    text, conf = transcribe_wav(args.path)
    print_json({"ok": True, "text": text, "confidence": round(conf, 3)})


def cmd_weekly(args) -> None:
    from .api.report import weekly_report
    import asyncio
    print_json(asyncio.run(weekly_report(user_id=args.user_id, days=args.days)))


def cmd_analytics(_) -> None:
    from .core.analytics import health_analyzer
    print_json(health_analyzer())


def cmd_wake_test(args) -> None:
    """命令行自检语音唤醒（KWS）：给一段 16k 单声道 WAV，看是否命中唤醒词。

    真机/树莓派上的实时唤醒走浏览器（设备屏页常开监听），这里只做离线验证。
    """
    import wave

    import numpy as np

    from .voice.sherpa_kws import kws_detector

    if not kws_detector.available():
        print_json({"ok": False, "error": "唤醒模型缺失，请运行 "
                    "python tools/download_sherpa_models.py"})
        return
    with wave.open(str(args.path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != config.ASR_SAMPLE_RATE:
            print_json({"ok": False, "error": "需要 16k 单声道 WAV"})
            return
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    words = ["你好饭崽", "你好正念"]
    step = int(config.ASR_SAMPLE_RATE * 0.2)
    for i in range(0, len(pcm), step):
        r = kws_detector.feed("cli", pcm[i:i + step].tobytes(), words=words)
        if r.get("matched"):
            print_json({"ok": True, "matched": True, "word": r.get("word")})
            return
    print_json({"ok": True, "matched": False, "note": "未命中（换个说法或检查音频）"})


COMMANDS = {
    "serve": cmd_serve, "init-db": cmd_init_db, "self-test": cmd_self_test,
    "ai-config": cmd_ai_config, "ai-chat": cmd_ai_chat, "tts": cmd_tts,
    "transcribe": cmd_transcribe, "weekly-review": cmd_weekly,
    "analytics": cmd_analytics, "wake-test": cmd_wake_test,
    "cleanup": cmd_cleanup,
}


def main() -> None:
    args = build_parser().parse_args()
    config.PATHS.ensure_dirs()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
