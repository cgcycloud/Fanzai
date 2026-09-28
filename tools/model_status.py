"""一次性脚本：汇总当前 ASR / LLM / TTS 的实际配置与模型文件。"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.voice import tts  # noqa: E402
from app.voice.asr import active_engine as asr_engine  # noqa: E402

print("================ ASR（语音转文字）================")
print("  引擎:", asr_engine())
print("  模型目录:", config.SHERPA_MODEL_DIRNAME)
d = config.PATHS.models_dir / config.SHERPA_MODEL_DIRNAME
files = {p.name: round(p.stat().st_size / 1048576, 1) for p in d.glob("*.onnx")} if d.exists() else {}
print("  文件:", files)

print("\n================ LLM（对话大脑）================")
key_env = os.environ.get("ZHIPU_API_KEY", "").strip()
url_env = os.environ.get("ZHIPU_API_URL", "").strip()
model_env = os.environ.get("ZHIPU_MODEL", "").strip()
print("  环境变量: api_key={} api_url={} model={}".format(
    "已设置" if key_env else "未设置", url_env or "（默认）", model_env or "（默认）"))
try:
    from app.core.ai_config import AIConfigStore
    cfg = AIConfigStore().load()
    if cfg is not None and cfg.key_set:
        print("  配置文件: api_url={} model={} 密钥已加密保存".format(cfg.api_url, cfg.model))
    else:
        print("  配置文件: 未配置真实 AI Key → 走本地规则回复（LocalEchoClient）")
except Exception as exc:
    print("  读取 AI 配置失败:", exc)
try:
    from app.core.ai_client import get_ai_client
    client = get_ai_client()
    print("  实际客户端:", type(client).__name__,
          f"(model={getattr(client, 'model', '?')})")
except Exception as exc:
    print("  获取客户端失败:", exc)

print("\n================ TTS（发声）================")
from app.core import interaction_config as ic  # noqa: E402
from app.voice import qwen_tts_engine  # noqa: E402
s = ic.get_settings()
print("  语速设置: {}%".format(s.speech_rate))
st = qwen_tts_engine.status()
print("  引擎: {} ({})".format(tts.active_engine(), st["model"]))
print("  接口: {}".format(st["api_base"]))
print("  音色: {}".format(st["voice"]))
print("  Key 已配置: {}  可用: {}".format(st["configured"], st["available"]))
if not st["configured"]:
    print("  !! 未配置 TTS API Key —— 请在网页设置页填入阿里云百炼的 Key")
if st["last_error"]:
    print("  最近错误:", st["last_error"])
