"""设置 API —— AI 配置（单一可手改文件 data_local/ai_config.json；公开视图绝不含密钥）+ 连通性测试 + 食物识别。

注意：**没有配置界面**（原 /admin 后台已删除）。这些接口保留下来给测试与脚本用，
日常改配置直接编辑 data_local/ai_config.json 即可（字段说明见 data_local/AI_CONFIG.md）。
"""
from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile

from ..core.ai_client import LocalEchoClient, get_ai_client
from ..core.ai_config import AIConfigStore
from ..vision.food import FOOD_DB_RAW, init_food_db, identify_food_glm4v

router = APIRouter(prefix="/api")


@router.get("/config")
async def get_config():
    return AIConfigStore().public_dict()


@router.post("/config")
async def save_config(request: Request):
    body = await request.json()
    config = AIConfigStore().save(
        api_url=str(body.get("api_url") or "").strip(),
        model=str(body.get("model") or "").strip(),
        api_key=(str(body["api_key"]).strip() if body.get("api_key") else None),
        system_prompt=(str(body["system_prompt"]).strip() if body.get("system_prompt") else None),
        tts_api_url=(str(body["tts_api_url"]).strip() if body.get("tts_api_url") else None),
        tts_api_key=(str(body["tts_api_key"]).strip() if body.get("tts_api_key") else None),
        tts_model=(str(body["tts_model"]).strip() if body.get("tts_model") else None),
        tts_voice=(str(body["tts_voice"]).strip() if body.get("tts_voice") else None),
        tts_language=(str(body["tts_language"]).strip() if body.get("tts_language") else None),
        tts_format=(str(body["tts_format"]).strip() if body.get("tts_format") else None),
        vision_model=(str(body["vision_model"]).strip() if body.get("vision_model") else None),
    )
    # 改了 TTS 端点/Key 后清掉熔断，用户不用干等 2 分钟
    if body.get("tts_api_url") or body.get("tts_api_key"):
        try:
            from ..voice import qwen_tts_engine
            qwen_tts_engine.reset_breaker()
        except Exception:
            pass
    try:
        from ..display.state import display_state
        display_state.set_ai_configured(bool(config.api_url and config.key_set))
    except Exception:
        pass
    return {"ok": True, "api_url": config.api_url, "model": config.model, "key_set": config.key_set}


@router.post("/config/test")
async def test_config():
    """连通性测试：当前配置下发一条短对话。"""
    try:
        client = get_ai_client()
        if isinstance(client, LocalEchoClient):
            return {"ok": False, "reply": client.chat("测试"),
                    "note": "当前使用本地规则回复（未配置真实 AI）"}
        reply = client.chat("请用一句话回复：连接测试")
        return {"ok": True, "reply": reply}
    except Exception as exc:
        return {"ok": False, "reply": f"连接失败: {exc}"}


@router.get("/food/database")
async def food_database():
    """55 种中餐热量库。"""
    init_food_db()
    return {"count": len(FOOD_DB_RAW), "items": [
        {"food_name": i.food_name, "calorie_per_100g": i.calorie_per_100g,
         "avg_density": i.avg_density} for i in FOOD_DB_RAW]}


@router.post("/food/identify")
async def food_identify(file: UploadFile = File(...)):
    """拍照识别食物 + 热量（GLM-4V 云端接口；未配置 AI 返回明确提示）。"""
    import tempfile
    raw = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        return identify_food_glm4v(tmp_path)
    finally:
        import os
        os.unlink(tmp_path)
