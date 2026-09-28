"""单元测试：核心层关键行为（pytest tests/ -q）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------- 时间戳时区（写入 UTC / 查询本地 曾错开 8 小时） ----------------
def test_event_range_query_is_timezone_safe(tmp_path):
    """查询用的本地 datetime 必须能匹配到写入时的 UTC 时间戳。

    这个 bug 真出现过：事件写入用 `utc_now()`（UTC，带 Z），而 analytics/report
    用 `datetime.now()`（本地）拼查询范围，两者直接做**字符串**比较 →
    本机 UTC+8 时整整错开 8 小时，于是"刚结束一餐，用餐次数还是 0"、
    最近 8 小时的情绪/语速/宵夜统计全部为空。
    """
    from datetime import datetime, timedelta

    from app.core.db import HealthStore, to_utc_str

    store = HealthStore(tmp_path / "tz.db")
    store.init()
    store.add_event("food_residual", {"user_id": "u", "meal_finished": True},
                    status="local")

    # 用本地时间（naive）查刚刚写入的事件 —— 必须查得到
    rows = store.query_event_range("food_residual",
                                   datetime.now() - timedelta(minutes=5),
                                   datetime.now() + timedelta(minutes=5))
    assert len(rows) == 1, "本地时间范围查不到刚写入的事件（时区错位）"

    # 用带时区的 UTC 时间查同样要能查到
    from datetime import timezone
    rows2 = store.query_event_range("food_residual",
                                    datetime.now(timezone.utc) - timedelta(minutes=5),
                                    datetime.now(timezone.utc) + timedelta(minutes=5))
    assert len(rows2) == 1, "UTC 时间范围也应能查到"

    # 归一函数本身：naive 视为本地、带时区换算成 UTC、字符串原样返回
    assert to_utc_str("2026-01-01T00:00Z") == "2026-01-01T00:00Z"
    assert to_utc_str(datetime.now()).endswith("Z")
    assert to_utc_str(None) == ""


# ---------------- 一餐的生命周期（餐前/餐中/餐后 + 只统计一次） ----------------
def test_meal_intent_detection():
    """意图识别要认得出"开饭/开吃/吃完了"，又不误判带否定词的说法。"""
    from app.core.meal import (wants_begin_eating, wants_finish_meal,
                               wants_start_meal)

    assert wants_start_meal("我要吃饭了") and wants_start_meal("开饭")
    assert not wants_start_meal("我今天不吃饭了")
    assert not wants_start_meal("我还没吃饭")
    assert not wants_start_meal("这个菜里放了什么调料啊你觉得呢")   # 长句不误判

    assert wants_begin_eating("我开吃了") and wants_begin_eating("开始吃了")
    assert not wants_begin_eating("我还没开始吃")

    assert wants_finish_meal("我吃完了") and wants_finish_meal("吃饱了")
    assert wants_finish_meal("不吃了谢谢")
    assert not wants_finish_meal("我还没吃完")
    assert not wants_finish_meal("没有吃完呢")


def test_meal_phase_progresses_by_signals_not_turn_count():
    """阶段要由真实信号推进：说"开吃"才进餐中，说"吃完了"才进餐后。

    旧实现每收一条消息就前进一步，6 句之后自动变"餐中" —— 跟吃没吃无关。
    """
    from app.core.meal import MealPhase, meal_manager

    uid = "pytest-meal-phase"
    meal_manager.reset(uid)
    s = meal_manager.start(uid)
    assert s.phase is MealPhase.PRE

    # 聊很多轮也不会自己跑进"餐中"
    for _ in range(8):
        meal_manager.note_turn(uid)
    assert meal_manager.current(uid).phase is MealPhase.PRE, "轮数不该推进阶段"

    meal_manager.to_mid(uid, reason="user")
    assert meal_manager.current(uid).phase is MealPhase.MID
    meal_manager.to_post(uid, reason="user_said_finished")
    assert meal_manager.current(uid).phase is MealPhase.POST
    meal_manager.reset(uid)


def test_meal_starts_on_vision_when_eating_detected():
    """餐前连续看到"在吃" → 自动进入餐中（不用用户开口）。"""
    from app.core.meal import AUTO_EAT_SUSTAIN_SEC, MealPhase, meal_manager

    uid = "pytest-meal-vision"
    meal_manager.reset(uid)
    meal_manager.start(uid)
    eating = {"face_count": 1, "chews_per_min": 20, "bites_per_min": 3}

    meal_manager.note_metrics(uid, eating)          # 第一次看到 → 开始计时
    assert meal_manager.current(uid).phase is MealPhase.PRE

    # 把"看到在吃"的起点往前挪，模拟已经持续够久
    meal_manager.current(uid).eating_seen_since -= (AUTO_EAT_SUSTAIN_SEC + 1)
    meal_manager.note_metrics(uid, eating)
    assert meal_manager.current(uid).phase is MealPhase.MID

    # 没人 / 没在咀嚼都不算在吃
    meal_manager.reset(uid)
    meal_manager.start(uid)
    meal_manager.note_metrics(uid, {"face_count": 0, "chews_per_min": 25})
    assert meal_manager.current(uid).phase is MealPhase.PRE
    meal_manager.reset(uid)


def test_meal_finish_is_idempotent():
    """**统计只做一次**：无论被话术、超时还是接口重复触发。"""
    from app.core.meal import meal_manager

    uid = "pytest-meal-once"
    meal_manager.reset(uid)
    meal_manager.start(uid)
    meal_manager.to_mid(uid)
    first = meal_manager.finish(uid, reason="user_said_finished")
    second = meal_manager.finish(uid, reason="auto_timeout")
    assert first is not None, "第一次收尾必须返回会话（调用方据此落库）"
    assert second is None, "重复收尾必须返回 None，否则会重复统计"
    assert not meal_manager.is_active(uid)


def test_meal_closes_after_ten_minutes_without_activity():
    """**没人吃、也没说话满 10 分钟 → 收尾统计**（用户直接走了 / 问了他也不回应）。

    `last_activity` 只在"检测到进食"或"有一次对话轮次"时刷新，
    所以坐着发呆、离开画面、不吭声都会让它到点 —— 用完餐次数不会漏记。
    另有 45 分钟整餐上限兜底（一直在吃、一直在聊的情况）。
    """
    import time as _time

    from app.core.meal import AUTO_SILENT_SEC, meal_manager

    uid = "pytest-meal-silent"
    meal_manager.reset(uid)
    meal_manager.start(uid)
    meal_manager.to_mid(uid)
    try:
        assert meal_manager.expire_reason(uid) == ""            # 刚开始，不用收
        meal_manager.current(uid).last_activity = _time.time() - (AUTO_SILENT_SEC - 5)
        assert meal_manager.expire_reason(uid) == "", "还没到 10 分钟不该收"
        meal_manager.current(uid).last_activity = _time.time() - (AUTO_SILENT_SEC + 1)
        assert meal_manager.expire_reason(uid) == "auto_silent"
        assert meal_manager.expired(uid) is True

        # 说一句话 → 计时刷新
        meal_manager.note_turn(uid)
        assert meal_manager.expire_reason(uid) == ""
        # 吃一口（视觉看到进食）→ 计时也刷新
        meal_manager.current(uid).last_activity = _time.time() - (AUTO_SILENT_SEC + 1)
        meal_manager.note_metrics(uid, {"vision": True, "face_count": 1, "chews_per_min": 20})
        assert meal_manager.expire_reason(uid) == ""

        # 整餐上限兜底：一直在吃也不会挂过 45 分钟
        meal_manager.current(uid).last_activity = _time.time()
        meal_manager.current(uid).started_at = _time.time() - (45 * 60 + 1)
        assert meal_manager.expire_reason(uid) == "auto_timeout"
    finally:
        meal_manager.reset(uid)


def test_cloud_vision_skips_mock_frames():
    """**模拟画面不打云端视觉**：没摄像头时画面是程序画的彩色条纹，
    发给 GLM-4V 只会白花额度、还会把"彩色条纹"当场景写进食物卡片。"""
    from app.vision.service import VisionService

    svc = VisionService.__new__(VisionService)
    svc._mode = "mock"
    svc._last_frame_jpeg = b"jpeg-bytes"
    assert VisionService._cloud_vision_due(svc) is False
    svc._mode = "webcam"
    svc._last_frame_jpeg = b""
    assert VisionService._cloud_vision_due(svc) is False, "还没拿到帧时也别打"
    svc._last_frame_jpeg = b"jpeg-bytes"
    assert VisionService._cloud_vision_due(svc) is True


def test_vision_food_result_gets_calories_and_nutrition():
    """视觉识别出的食物要带上卡路里/能量/营养，并优先用内置热量库校准。

    模型只负责"看到什么菜、大概多少克"；热量优先取 55 种中餐热量库的
    kcal/100g × 克数（`source="db"`），库里没有才用模型的估算（`source="model"`）。
    """
    from app.vision.service import VisionService

    svc = VisionService.__new__(VisionService)          # 只测纯函数，不起摄像头
    food = VisionService._with_nutrition({
        "description": "米饭和红烧肉",
        "items": [
            {"name": "一碗白米饭", "portion_g": 150, "kcal": 999},      # 库里有 → 116/100g
            {"name": "红烧肉", "portion_g": 80, "kcal": 300, "protein_g": 12, "fat_g": 25},
            {"name": "某不存在的私房菜", "portion_g": 100, "kcal": 123},
        ],
    })
    items = food["items"]
    assert len(items) == 3
    rice = items[0]
    assert rice["kcal"] == 174 and rice["energy_kj"] == 728, rice   # 116×1.5g → 174 kcal
    assert rice["source"] == "db" and rice["kcal_per_100g"] == 116
    assert items[1]["source"] == "db" and items[1]["kcal"] == 316   # 395×0.8
    assert items[2]["source"] == "model" and items[2]["kcal"] == 123  # 库里没有就信模型
    assert food["kcal_total"] == 174 + 316 + 123
    assert food["energy_kj"] == round((174 + 316 + 123) * 4.184)
    assert food["protein_g"] == 12.0 and food["fat_g"] == 25.0
    assert food["calorie_estimate_kcal"] == food["kcal_total"], "兼容旧卡片的字段"

    # 旧格式（只有一句描述 + 一个总热量）也要能出能量
    legacy = VisionService._with_nutrition({"description": "看不清", "calorie_estimate_kcal": 300})
    assert legacy["energy_kj"] == round(300 * 4.184)


def test_camera_page_renders_food_strip():
    """摄像头页要把食物信息条（卡路里/能量）画出来，而且**首屏就能看见**。

    两个互相牵制的需求（都来自用户反馈）：
      * 画面要够大够清楚 → 画面保持 640×360（16:9 原始比例）
      * "识别出食物就能看到热量" → 信息条必须完整落在首屏 480px 内
    所以热量被压成两行信息条（66→58px），而不是占一张大卡片。
    """
    from PIL import Image, ImageDraw

    from app.display import render
    from app.display.state import DisplayState

    st = DisplayState()
    st.set_metrics(chews_per_min=24.0, chew_level="normal", chew_count=137,
                   bites_per_min=6.0, eat_level="normal", emotion="放松",
                   face_count=1, vision=True)
    _, h_empty = render._page_camera(st, 640, 480, 0.0, None, {})
    food = {
        "description": "白米饭、红烧肉",
        "items": [
            {"name": "白米饭", "portion_g": 150, "kcal": 174, "energy_kj": 728},
            {"name": "红烧肉", "portion_g": 80, "kcal": 316, "energy_kj": 1322},
            {"name": "西兰花", "portion_g": 100, "kcal": 34, "energy_kj": 142},
        ],
        "kcal_total": 524, "energy_kj": 2192,
        "carb_g": 40.0, "protein_g": 16.0, "fat_g": 25.5,
    }
    content, h_food = render._page_camera(st, 640, 480, 0.0, None, {"food": food})
    assert content.size == (640, 780)
    assert h_food >= 700, f"内容高度异常：{h_food}"

    # 信息条高度固定（两行），不会因为条数把画面挤下去
    canvas = Image.new("RGB", (640, 500), (0, 0, 0))
    d = ImageDraw.Draw(canvas)
    y_none = render._food_strip(d, 640, 0, {})
    y_three = render._food_strip(d, 640, 0, {"items": food["items"], "kcal_total": 524})
    assert y_none == y_three == 58, (y_none, y_three)

    # 首屏可见性：头 40 + 画面 360 + 间距 10 + 信息条 58 → 下沿 468 ≤ 480
    strip_top = 40 + 10 + 360 + 10
    assert strip_top + y_three <= 480, f"食物信息条落在首屏外：下沿 {strip_top + y_three}"


def test_meal_auto_starts_from_vision_when_nobody_said_start():
    """没喊"开饭"、但摄像头连续看到在吃 → 自动开一餐（直接餐中）。

    否则"没人喊开饭就不存在这一餐"，用餐次数永远统计不到。
    只有连续够久才算：筷子晃一下、人在镜头前站一会儿不该凭空多出一餐。
    """
    from app.core.meal import AUTO_EAT_SUSTAIN_SEC, MealPhase, meal_manager

    uid = "pytest-meal-autostart"
    meal_manager.reset(uid)
    eating = {"face_count": 1, "chews_per_min": 20, "bites_per_min": 3}

    # 单帧看到"在吃"不能开餐（先去抖）
    meal_manager.note_metrics(uid, eating)
    assert not meal_manager.is_active(uid)

    # 连续够久 → 直接开在餐中（用户已经在吃了，不需要餐前引导）
    with meal_manager._lock:
        meal_manager._watching[uid] -= (AUTO_EAT_SUSTAIN_SEC + 1)
    meal_manager.note_metrics(uid, eating)
    meal = meal_manager.current(uid)
    assert meal is not None and meal.phase is MealPhase.MID
    assert meal.started_by == "vision"

    # 没看到人在吃 → 清掉计时，不会误开
    meal_manager.reset(uid)
    for _ in range(3):
        meal_manager.note_metrics(uid, {"face_count": 0, "chews_per_min": 30})
    assert not meal_manager.is_active(uid)


def test_autonomy_feeds_meal_state_even_when_proactive_off(monkeypatch):
    """自主互动关掉也应继续维护这一餐（开餐/超时收尾），否则用餐次数永远统计不到。"""
    from app.core.autonomy import AutonomyService
    from app.core import interaction_config as ic
    from app.core.meal import MealPhase, meal_manager

    uid = "default"
    meal_manager.reset(uid)
    monkeypatch.setattr(ic, "get_settings",
                        lambda: ic.AgentSettings(autonomy=ic.AUTONOMY_OFF))
    monkeypatch.setattr(AutonomyService, "_metrics_snapshot",
                        staticmethod(lambda: {"face_count": 1, "chews_per_min": 25}))
    try:
        svc = AutonomyService()
        svc._tick()                                     # 第一次看到 → 开始计时
        assert not meal_manager.is_active(uid)
        with meal_manager._lock:
            meal_manager._watching[uid] -= 30           # 假装已经连续看到 30 秒
        svc._tick()
        meal = meal_manager.current(uid)
        assert meal is not None and meal.phase is MealPhase.MID, "关掉自主互动也要能自动开餐"
        assert meal.started_by == "vision"
    finally:
        meal_manager.reset(uid)


def test_tts_switch_off_keeps_text_only(tmp_path, monkeypatch):
    """关掉「语音播报」后：回复文本照常推，但一个音频事件都不该有（也不打云端 TTS）。"""
    import json

    from fastapi.testclient import TestClient

    import app.api.dialogue as dlg
    from app.core import interaction_config as ic
    from app.server import app

    store = ic.AgentSettingsStore(tmp_path / "s.json")
    store.save(mode=ic.MODE_MEAL, tts_enabled=False)
    monkeypatch.setattr(ic, "agent_settings_store", store)

    class FakeClient:
        def chat_stream(self, prompt, context=None, user_text=None):
            yield "先喝口水，慢慢吃。"

        def chat(self, prompt, context=None, **kw):
            return "先喝口水，慢慢吃。"

    monkeypatch.setattr(dlg, "get_ai_client", lambda: FakeClient())

    events, texts = [], []
    with TestClient(app) as client:
        with client.stream("POST", "/api/dialogue/stream",
                           json={"message": "这个菜有点咸", "user_id": "pytest-tts-off"}) as r:
            ev = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                    events.append(ev)
                elif line.startswith("data:") and ev == "reply":
                    texts.append(json.loads(line[5:].strip()).get("text", ""))
    assert "".join(texts).strip(), "关了播报也要有文字"
    assert "audio" not in events, "关了播报不该再推音频事件"
    assert ic.tts_enabled() is False


def test_mode_auto_switch_on_meal_start_and_end(tmp_path, monkeypatch):
    """日常聊天里说「我要吃饭」→ 自动切进食检测；这一餐结束 → 自动切回日常聊天。

    切模式必须**保留用户选的自主互动级别**（手动切模式才会重置成默认）。
    """
    import app.api.dialogue as dlg
    from app.core import interaction_config as ic
    from app.core.meal import meal_manager

    store = ic.AgentSettingsStore(tmp_path / "s.json")
    store.save(mode=ic.MODE_CHAT, autonomy=ic.AUTONOMY_OFF)
    monkeypatch.setattr(ic, "agent_settings_store", store)
    applied = {"n": 0}
    monkeypatch.setattr("app.api.agent.apply_settings",
                        lambda settings=None: applied.__setitem__("n", applied["n"] + 1))

    uid = "pytest-mode-switch"
    meal_manager.reset(uid)
    try:
        assert ic.get_settings().mode == ic.MODE_CHAT
        phase, _finished, _reason = dlg._advance_meal(uid, "我要吃饭了")
        assert phase == "pre_meal"
        assert ic.get_settings().mode == ic.MODE_MEAL, "说要吃饭应自动切进食检测"
        assert applied["n"] == 1, "切模式要落实到硬件（开摄像头）"

        meal_manager.to_mid(uid)
        dlg._auto_switch_mode(ic.MODE_CHAT, "test：用餐结束")
        s = ic.get_settings()
        assert s.mode == ic.MODE_CHAT, "用餐结束应切回日常聊天"
        assert s.autonomy == ic.AUTONOMY_OFF, "自动切模式不能覆盖用户选的自主互动级别"
    finally:
        meal_manager.reset(uid)


def test_meal_flow_over_dialogue_api(tmp_path, monkeypatch):
    """整条链路走真实接口跑一遍：说吃饭→餐前；说开吃→餐中；说吃完了→餐后并只统计一次。

    这条用例专门钉住"按对话轮数推进"和"关键词乱收尾"两个回归：
      * 旧状态机每收一条消息就前进一步，13 轮后自动判定"结束用餐"并落一次统计；
      * 旧 `said_finished` 把"谢谢""结束"当结束语 → 一句"谢谢，这个菜真好吃"
        就能把一餐提前收掉。
    """
    from fastapi.testclient import TestClient

    import app.api.dialogue as dlg
    from app.core.db import HealthStore
    from app.core.meal import MealPhase, meal_manager
    from app.server import app

    uid = "pytest-meal-flow"
    meal_manager.reset(uid)
    store = HealthStore(tmp_path / "flow.db")
    store.init()
    monkeypatch.setattr(dlg, "HealthStore", lambda *a, **k: store)

    class FakeStreamClient:
        def chat_stream(self, prompt, context=None, user_text=None):
            yield "好的，我们慢慢来。"

        def chat(self, prompt, context=None, **kw):
            return "好的，我们慢慢来。"

    monkeypatch.setattr(dlg, "get_ai_client", lambda: FakeStreamClient())

    def say(client, text) -> dict:
        with client.stream("POST", "/api/dialogue/stream",
                           json={"message": text, "user_id": uid}) as resp:
            done = {}
            for line in resp.iter_lines():
                if line.startswith("data:") and "finished" in line:
                    import json as _json
                    done = _json.loads(line[5:].strip())
        return done

    with TestClient(app) as client:
        assert meal_manager.current(uid) is None

        # 1) 说要吃饭 → 开一餐（餐前）
        assert say(client, "我要吃饭了")["phase"] == "pre_meal"
        assert meal_manager.current(uid).phase is MealPhase.PRE

        # 2) 普通聊天不推进阶段，也不会被"谢谢/结束"这类词误收尾
        for text in ("今天做了个番茄炒蛋", "谢谢，这个菜闻着真香", "结束了做饭好累"):
            done = say(client, text)
            assert done["finished"] is False, f"「{text}」不该结束这一餐"
        assert meal_manager.current(uid).phase is MealPhase.PRE

        # 3) 说开吃 → 餐中
        assert say(client, "我开吃了")["phase"] == "mid_meal"
        assert meal_manager.current(uid).phase is MealPhase.MID

        # 4) 聊满 14 轮也不会因为"轮数到了"就收尾（旧状态机 13 轮自动结束）
        for i in range(14):
            done = say(client, f"第{i}口，慢慢嚼")
            assert done["finished"] is False, "轮数不该把一餐收掉"
        assert meal_manager.current(uid).phase is MealPhase.MID

        # 5) 说吃完了 → 餐后收尾，且只落一次统计
        done = say(client, "我吃完了")
        assert done["finished"] is True and done["phase"] == "post_meal"
        assert meal_manager.current(uid) is None

    from datetime import datetime, timedelta
    lo, hi = datetime.now() - timedelta(hours=1), datetime.now() + timedelta(hours=1)
    food = store.query_event_range("food_residual", lo, hi)
    summ = store.query_event_range("meal_summary", lo, hi)
    assert len(food) == 1 and len(summ) == 1, f"应各落一条：{len(food)}/{len(summ)}"
    assert food[0]["started_ts"], "要带用餐起点（分析用它算用餐间隔）"
    assert summ[0]["total_turns"] > 0


def test_close_meal_records_both_events_exactly_once(tmp_path):
    """收尾要同时写 food_residual（算用餐次数）与 meal_summary，且各只一条。"""
    import asyncio

    from app.api.dialogue import _close_meal
    from app.core.db import HealthStore
    from app.core.meal import meal_manager

    uid = "pytest-meal-record"
    meal_manager.reset(uid)
    store = HealthStore(tmp_path / "m.db")
    store.init()

    meal_manager.start(uid)
    meal_manager.to_mid(uid)
    assert _close_meal(store, uid, reason="user_said_finished") is True
    # 再调一次不应该再落库
    assert _close_meal(store, uid, reason="auto_timeout") is False

    from datetime import datetime, timedelta
    start, end = datetime.now() - timedelta(days=1), datetime.now() + timedelta(days=1)
    food = store.query_event_range("food_residual", start, end)
    summ = store.query_event_range("meal_summary", start, end)
    assert len(food) == 1, f"food_residual 应只有 1 条，实际 {len(food)}"
    assert len(summ) == 1, f"meal_summary 应只有 1 条，实际 {len(summ)}"
    assert food[0].get("meal_finished") is True
    assert food[0].get("started_ts"), "要带用餐起点，分析要用它算间隔"


def test_meal_phase_guide_is_injected_into_prompt():
    """阶段提示必须真的进到 LLM 提示里，而且是**行为要求**而非只报阶段名。"""
    from app.api.dialogue import _build_llm_prompt
    from app.core.meal import PHASE_GUIDE, MealPhase

    for phase in (MealPhase.PRE, MealPhase.MID, MealPhase.POST):
        p = _build_llm_prompt("我在吃呢", phase.value, meal_active=True)
        assert PHASE_GUIDE[phase][:20] in p, f"{phase} 的阶段要求没进提示"
    # 没有进行中的一餐时，不得注入任何阶段要求（普通聊天不该被拉回进食话题）
    idle = _build_llm_prompt("今天天气不错", "idle", meal_active=False)
    assert PHASE_GUIDE[MealPhase.MID][:20] not in idle
    assert "餐中" not in idle


def test_autonomy_defers_to_busy_user():
    """用户正在说话/机器人正在播报时，AI 不得主动开口（防插嘴/打断）。"""
    from app.core.autonomy import autonomy_service
    from app.display.state import display_state

    display_state.set_listening(False)
    display_state.set_busy("test", False)
    try:
        display_state.end_speaking()
        assert autonomy_service._can_speak() is True
        display_state.set_listening(True)          # 用户正在说话
        assert autonomy_service._can_speak() is False
        display_state.set_listening(False)
        display_state.set_busy("speaking", True, ttl=30)   # 正在播报
        assert autonomy_service._can_speak() is False
        display_state.set_busy("speaking", False)
        assert autonomy_service._can_speak() is True
    finally:
        display_state.set_listening(False)
        display_state.set_busy("speaking", False)
        display_state.set_busy("test", False)


def test_autonomy_prompt_includes_current_state():
    """主动开口的提示必须带上当前状态（阶段 + 摄像头 + 时间），否则容易说不合时宜的话。"""
    from app.core.autonomy import autonomy_service
    from app.core.meal import meal_manager
    from app.core import interaction_config as ic

    uid = "default"
    meal_manager.reset(uid)
    meal_manager.start(uid)
    meal_manager.to_mid(uid)
    try:
        prompt = autonomy_service._build_prompt(
            ic.get_settings().mode_info, ic.TRIGGER_PROMPTS["chew_fast"],
            {"emotion": "焦虑", "chews_per_min": 30, "chew_level": "fast",
             "face_count": 1, "chew_count": 42})
        assert "餐中" in prompt, "要带上这一餐的阶段"
        assert "焦虑" in prompt and "30" in prompt, "要带上摄像头观察到的状态"
        assert "次/分" in prompt
    finally:
        meal_manager.reset(uid)


# ---------------- 状态机 ----------------
def test_state_machine_full_flow():
    from app.core.state_machine import DialogueState, DialogueStateMachine

    sm = DialogueStateMachine()
    sm.start_full_meal_session()
    assert sm.state is DialogueState.PRE_MEAL_BREATH
    assert sm.phase == "pre_meal"
    while sm.state is not DialogueState.END:
        sm.process_user_input("继续")
    assert sm.summary_dict()["total_turns"] > 0


def test_state_machine_empty_message_does_not_advance():
    from app.core.state_machine import DialogueState, DialogueStateMachine

    sm = DialogueStateMachine()
    sm.start_full_meal_session()
    before = sm.state
    reply, finished = sm.process_user_input("")
    assert sm.state is before and not finished
    assert reply == "让我们先做三次深呼吸，放松一下。"


def test_state_machine_end_keyword_any_state():
    from app.core.state_machine import DialogueState, DialogueStateMachine

    sm = DialogueStateMachine()
    sm.start_full_meal_session()
    for kw in ("结束", "谢谢", "可以了", "再见"):
        sm.start_full_meal_session()
        _, finished = sm.process_user_input(f"好的{kw}")
        assert finished and sm.state is DialogueState.END


# ---------------- 配置：单一可手改文件 ----------------
def test_api_key_plaintext_and_hand_editable():
    """Key 现在是**明文**存在 ai_config.json 里，为的是能直接手改这个文件。

    这是有意的设计变更：原来用 Fernet 加密（并把本机密钥放在 ai_config.key），
    但那样用户没法手写/替换 Key。改为明文后：
      * 文件本身仍设为仅本用户可读写（Windows 上 chmod 无效，故不断言权限）
      * public_dict() 依旧**不外泄** Key（给接口/日志用）
      * 旧的 encrypted_* 字段仍能读，保证老配置不失效
    """
    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        store = AIConfigStore(Path(td) / "c.json", Path(td) / "k.key")
        store.save("https://x/v4", "m", api_key="sk-secret-abc", tts_api_key="sk-tts-xyz")
        raw = (Path(td) / "c.json").read_text(encoding="utf-8")
        assert "sk-secret-abc" in raw and "sk-tts-xyz" in raw, "明文保存才可手改"
        # 不再写旧的加密字段
        assert "encrypted_api_key" not in raw
        loaded = store.load()
        assert loaded.api_key == "sk-secret-abc"
        assert loaded.tts_api_key == "sk-tts-xyz"
        assert loaded.key_set and loaded.tts_key_set


def test_vision_provider_can_differ_from_chat():
    """对话换厂商时视觉要能留在原厂商（否则 glm-4v 会被发到 Qwen 端点上）。

    实际部署：对话 = 百炼 qwen3.7-flash，视觉 = 智谱 glm-4v-flash（每 10 秒一次）。
    vision_api_url / vision_api_key 留空则沿用对话那组（单厂商时不用填）。
    """
    import json

    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        store = AIConfigStore(Path(td) / "c.json", Path(td) / "k.key")
        store.save("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-flash",
                   api_key="sk-dash", vision_model="glm-4v-flash",
                   vision_api_url="https://open.bigmodel.cn/api/paas/v4",
                   vision_api_key="glm-key")
        cfg = store.load()
        assert cfg.model == "qwen3.7-flash" and cfg.api_key == "sk-dash"
        assert cfg.vision_connection == ("https://open.bigmodel.cn/api/paas/v4", "glm-key")
        # public_dict 只报"配没配"，不外泄 Key 本体
        pub = store.public_dict()
        assert pub["vision_api_url"].endswith("/v4") and pub["vision_key_set"] is True
        assert "glm-key" not in json.dumps(pub, ensure_ascii=False)

        # 不传 vision_* 时保留原值；单厂商场景下留空则自动沿用对话那组
        assert store.save("https://x/v1", "m2", api_key="k2").vision_api_key == "glm-key"
        single = store.save("https://y/v1", "m3", api_key="k3",
                            vision_api_url="", vision_api_key="")
        assert single.vision_connection == ("https://y/v1", "k3")


def test_memory_context_limit_follows_config_not_stale_file(tmp_path):
    """上下文上限只认 app/config.py，不被旧记忆文件里的 64k 按回去。

    两个数各管一件事：
      * `token_limit`（128k）= 硬上限，是"模型窗口"这个口径；
      * `usable_tokens`（发送预算 × 4 = 8k）= **摘要压缩线** —— 超过它就把旧对话
        压成摘要，保证每次真正发出去的窗口（最近 ~2k tokens）永远落在还留着
        原文的范围内，也保证每轮 prefill 恒定（否则 history 攒到 10 万 token，
        每轮首字要等几十秒）。
    """
    import json as _json

    from app.config import MEMORY_SEND_TOKENS, MEMORY_TOKEN_LIMIT
    from app.core.memory import ConversationMemory

    p = tmp_path / "conversation_memory_u.json"
    p.write_text(_json.dumps({
        "token_limit": 64_000, "reserve_tokens": 8_000, "keep_recent_turns": 8,
        "user_id": "u", "summary": "旧摘要", "turns": [],
    }, ensure_ascii=False), encoding="utf-8")

    mem = ConversationMemory.load("u", path=p)
    assert mem.token_limit == MEMORY_TOKEN_LIMIT == 128_000
    assert mem.usable_tokens == MEMORY_SEND_TOKENS * 4
    assert mem.usable_tokens < mem.token_limit, "压缩线必须远小于硬上限"
    assert mem.summary == "旧摘要" and mem.turns == []

    mem.save()                                     # 写回时丢掉过期的上限字段
    saved = _json.loads(p.read_text(encoding="utf-8"))
    assert "token_limit" not in saved and saved["summary"] == "旧摘要"


def test_memory_send_budget_keeps_recent_turns_only(tmp_path):
    """发出去的上下文受预算约束（首字延迟主要由 prefill 长度决定），
    但**最近说的话一定在里面**，而且记忆本身不会被裁剪。"""
    from app.config import MEMORY_SEND_TOKENS
    from app.core.memory import ConversationMemory, estimate_tokens

    mem = ConversationMemory(path=tmp_path / "m.json", user_id="u", summary="")
    for i in range(120):
        mem.turns.append({"user_text": f"第{i}轮用户说的话，随便聊几句凑点长度",
                          "assistant_reply": f"第{i}轮助手的回复，也有一定长度用来占用预算",
                          "phase": "mid_meal", "timestamp": "2026-09-16T00:00:00Z"})
    sent = mem.context_text()
    assert estimate_tokens(sent) <= MEMORY_SEND_TOKENS + 200, estimate_tokens(sent)
    assert "第119轮用户说的话" in sent, "最近一轮必须发出去"
    assert "第0轮用户说的话" not in sent, "太老的轮次不该再占预算"
    assert len(mem.turns) == 120, "记忆本身不被裁剪"
    assert estimate_tokens(mem.context_text(max_tokens=None)) > MEMORY_SEND_TOKENS


def test_tuning_json_overrides_context_and_fps(tmp_path, monkeypatch):
    """上下文长度等数值支持**外部覆盖**（发行版/exe 里改不了 config.py，只能靠这个）。

    优先级：环境变量 > data_local/tuning.json > 代码默认值；
    只认白名单键、超范围自动收缩、写错当没写（不能让服务起不来）。
    """
    import json as _json

    import app.config as cfg

    monkeypatch.setattr(cfg, "PATHS", type(cfg.PATHS)(
        data_dir=tmp_path, media_dir=tmp_path / "media", models_dir=tmp_path / "models",
        db_path=tmp_path / "x.db", ai_config_path=tmp_path / "ai.json",
        ai_key_path=tmp_path / "ai.key"))
    (tmp_path / "tuning.json").write_text(_json.dumps({
        "memory_send_tokens": 4000,
        "screen_camera_fps": 20,
        "kws_num_threads": 99,            # 超范围 → 收缩到上限 4
        "unknown_key": 123,               # 白名单外 → 忽略
        "memory_reserve_tokens": "abc",   # 写错 → 用默认值
    }), encoding="utf-8")

    monkeypatch.setattr(cfg, "_TUNING_APPLIED", {}, raising=False)
    assert cfg._tuned("memory_send_tokens", 2_000) == 4_000
    assert cfg._tuned("screen_camera_fps", 30) == 20
    assert cfg._tuned("kws_num_threads", 1) == 4
    assert cfg._tuned("memory_reserve_tokens", 8_000) == 8_000
    assert "unknown_key" not in cfg.tuning_applied()
    # 环境变量优先级更高
    monkeypatch.setenv("MINDFUL_MEMORY_SEND_TOKENS", "6000")
    assert cfg._tuned("memory_send_tokens", 2_000) == 6_000


def test_legacy_encrypted_key_still_readable():
    """老配置里手写的 encrypted_* 字段仍要能读（否则升级后全都没声音）。"""
    import json

    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        cfg_path, key_path = Path(td) / "c.json", Path(td) / "k.key"
        store = AIConfigStore(cfg_path, key_path)
        enc = store.encrypt_key("sk-legacy-1")
        enc_tts = store.encrypt_key("sk-legacy-2")
        cfg_path.write_text(json.dumps({
            "api_url": "https://x/v4", "model": "m",
            "encrypted_api_key": enc,
            "encrypted_tts_api_key": enc_tts,
        }), encoding="utf-8")
        loaded = store.load()
        assert loaded.api_key == "sk-legacy-1"
        assert loaded.tts_api_key == "sk-legacy-2"


def test_plaintext_key_wins_over_encrypted():
    """同时存在时以明文为准（用户手改的就是明文那个字段）。"""
    import json

    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        cfg_path, key_path = Path(td) / "c.json", Path(td) / "k.key"
        store = AIConfigStore(cfg_path, key_path)
        cfg_path.write_text(json.dumps({
            "api_url": "https://x/v4", "model": "m",
            "api_key": "sk-plain",
            "encrypted_api_key": store.encrypt_key("sk-legacy"),
        }), encoding="utf-8")
        assert store.load().api_key == "sk-plain"


def test_broken_json_reports_clearly():
    """手改文件写坏 JSON 时要给出明确报错，而不是抛一个看不懂的异常。"""
    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "c.json"
        cfg.write_text('{"api_url": "https://x/v4",,}', encoding="utf-8")
        store = AIConfigStore(cfg, Path(td) / "k.key")
        try:
            store.load()
            raise AssertionError("坏 JSON 应当报错")
        except RuntimeError as exc:
            assert "JSON" in str(exc)


def test_public_dict_hides_key():
    from app.core.ai_config import AIConfigStore

    with tempfile.TemporaryDirectory() as td:
        store = AIConfigStore(Path(td) / "c.json", Path(td) / "k.key")
        store.save("https://x/v4", "m", api_key="sk-secret-abc")
        public = store.public_dict()
        assert public["key_set"] is True
        assert "sk-secret-abc" not in str(public)


# ---------------- 数据库 ----------------
def test_db_event_roundtrip():
    from app.core.db import HealthStore

    with tempfile.TemporaryDirectory() as td:
        store = HealthStore(Path(td) / "t.db")
        store.init()
        store.add_event("food_residual", {"trigger_ts": 1, "eating_window_h": 5})
        rows = store.query_event_range("food_residual",
                                       __import__("datetime").datetime(2000, 1, 1),
                                       __import__("datetime").datetime(2100, 1, 1))
        assert len(rows) == 1 and rows[0]["eating_window_h"] == 5


# ---------------- 分析 ----------------
def test_analytics_shapes():
    from app.core.analytics import health_analyzer

    r = health_analyzer("t")
    assert set(r["week_report"]) >= {"meal_count", "adherence_score", "weight_trend"}
    assert "personal_insight" in r and "review_plan" in r
    assert r["review_plan"]["phase"] in ("强化期", "巩固期", "维持期")


def test_risk_thresholds_are_configurable():
    from app import config

    assert config.SPEECH_FAST_THRESH > config.SPEECH_SLOW_THRESH
    assert config.PHASE_CONFIG["intensive"]["interval_days"] == 14
    assert config.PHASE_CONFIG["maintain"]["interval_days"] == 90


# ---------------- 情绪提取 ----------------
def test_emotion_extraction_rules():
    from app.api.dialogue import extract_emotion_from_text

    assert extract_emotion_from_text("恭喜你！真为你开心") == "happy"
    assert extract_emotion_from_text("别难过，我陪着你") == "love"   # 安慰场景
    assert extract_emotion_from_text("我们现在开始吧") == "neutral"


# ---------------- 食物热量 ----------------
def test_food_database_and_estimate():
    from app.vision.food import FOOD_DB_RAW, query_food_calorie

    assert len(FOOD_DB_RAW) == 55
    item = query_food_calorie("白米饭")
    assert item["calorie_per_100g"] == 116 and 0 < item["avg_density"] <= 1.1


# ---------------- API 冒烟 ----------------
def test_api_smoke():
    from fastapi.testclient import TestClient
    from app.server import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"]
        assert client.get("/").status_code == 200
        d = client.post("/api/dialogue", json={"message": ""}).json()
        # 阶段以 meal_manager 为准（没有进行中的一餐就是 idle）；
        # 旧的 DialogueStateMachine 不再参与，也不再返回 state 字段
        assert d["ok"] and d["phase"] == "idle" and d["finished"] is False
        assert "configured" in client.get("/api/config").json()
        assert "personal_insight" in client.get("/api/analytics").json()
        assert client.get("/api/food/database").json()["count"] == 55


# ---------------- SSE 流式回归（防止事件被整体缓冲 / 顺序错乱） ----------------
def test_dialogue_stream_event_order():
    """SSE 事件顺序：reply 在前、audio 居中、done 最后。

    注意：这里只断言顺序。TestClient 的传输层会把整个响应缓冲到结束才交付
    （实测首个事件到达时间 == 总耗时），因此延迟必须在真实服务上测
    （uvicorn 下实测首句 0.05s、首句语音约 1.5~2s）。
    """
    import time
    from fastapi.testclient import TestClient
    from app.server import app

    with TestClient(app) as client:
        t0 = time.time()
        with client.stream("POST", "/api/dialogue/stream",
                           json={"message": "你好正念，我有点饿",
                                 "user_id": "pytest-order"}) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    events.append(line[6:].strip())
        elapsed = time.time() - t0

    assert events, "SSE 未产生任何事件"
    assert "reply" in events and "audio" in events and "done" in events, events
    assert events[0] == "reply", f"首个事件应为 reply: {events}"
    assert events[-1] == "done", f"done 必须最后: {events[-3:]}"
    assert events.index("audio") < events.index("done"), "音频必须在 done 之前发出"
    assert elapsed < 60, f"整体超时，疑似挂起: {elapsed:.1f}s"


def test_dialogue_stream_empty_message():
    """空消息不推进任何阶段，只回一句开场白；文案由 AI 生成，不念本地脚本。"""
    from fastapi.testclient import TestClient
    from app.server import app

    with TestClient(app) as client:
        with client.stream("POST", "/api/dialogue/stream",
                           json={"message": "", "user_id": "pytest-empty"}) as resp:
            body = "".join(resp.iter_text())
    assert "event: reply" in body and "event: done" in body
    assert '"phase": "idle"' in body          # 阶段未推进（没有进行中的一餐）
    assert "状态机引导语" not in body          # 不再输出本地脚本引导语


# ---------------- 本地规则回复相关性（防止答非所问） ----------------
def test_local_client_matches_user_text_not_prompt():
    """prompt 里含状态机引导语，本地规则必须只看用户原话。"""
    from app.core.ai_client import LocalEchoClient

    c = LocalEchoClient()
    # 引导语含"饥饿"，但用户说的是压力 → 必须回压力话术
    prompt = ("你是正念进食助手，正在为用户做餐前引导。状态机引导语是：你现在真的感到饥饿吗？"
              "请描述一下。\n用户刚才说：我现在压力很大，不太想吃饭\n请用温暖自然的中文回应。")
    r = c.chat(prompt, user_text="我现在压力很大，不太想吃饭")
    assert "深呼吸" in r and "不太好受" in r
    # 糖尿病场景（商业计划书红线：不替代药物）
    r2 = c.chat(prompt, user_text="我有糖尿病，这样吃会影响血糖吗")
    assert "不能替代" in r2 and "医生" in r2
    # 不传 user_text 时退回旧行为（整段匹配），不应崩溃
    assert c.chat(prompt)


def test_local_client_stream_accepts_user_text():
    from app.core.ai_client import LocalEchoClient

    c = LocalEchoClient()
    out = "".join(c.chat_stream("引导语：你饿吗", user_text="我吃饱了"))
    assert "饱" in out


# ---------------- 识别引擎（统一为 sherpa-onnx Paraformer） ----------------
def test_asr_engine_dispatch():
    """识别引擎只剩 Paraformer（旧的 Vosk 回退已从工程中删除）。"""
    from app.voice import asr, sherpa_asr

    engine = asr.active_engine()
    assert engine in ("paraformer", "unavailable"), engine
    if sherpa_asr.available():
        assert engine == "paraformer", f"模型已就位却未选用 Paraformer: {engine}"
    else:
        # 模型缺失时必须给出可执行的提示，而不是静默降级到已删除的引擎
        import pytest

        with pytest.raises(asr.ModelMissing):
            asr.transcribe_wav(Path("nope.wav"))


def test_sherpa_model_files_present():
    from app.voice import sherpa_asr

    if sherpa_asr.available():
        d = sherpa_asr.model_dir()
        assert (d / "model.int8.onnx").stat().st_size > 10_000_000
        assert (d / "tokens.txt").stat().st_size > 1000


def test_transcribe_real_speech_if_available():
    """有 Windows 中文 TTS 时：合成真实语音 → 识别 → 比对关键字符。"""
    import subprocess
    import tempfile
    from pathlib import Path as _P
    from app.voice.asr import transcribe_wav
    from app.voice.convert import to_16k_wav

    ps = _P("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not ps.exists():
        pytest.skip("非 Windows 环境，跳过真实语音回环测试")
    out = _P(tempfile.gettempdir()) / "pytest_asr_speech.wav"
    subprocess.run([str(ps), "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate=-1; "
                    f"$s.SetOutputToWaveFile('{out}'); $s.Speak('你好正念我有点饿'); $s.Dispose()"],
                   capture_output=True)
    if not out.exists():
        pytest.skip("系统无中文语音合成音色")
    conv = out.with_name("pytest_asr_speech_16k.wav")
    conv.write_bytes(to_16k_wav(out.read_bytes()))
    text, _ = transcribe_wav(conv)
    assert "饿" in text, f"识别结果不含关键词: {text}"


# ---------------- 定期保洁：缓存/日志/过期事件 ----------------
def test_cache_janitor_cleans_media_logs_and_old_events(tmp_path, monkeypatch):
    """定期保洁：媒体临时文件按时间清、日志超限只留尾部、过期高频事件删掉。

    设备是长期无人值守跑的，这三处都会自己长胖（详见 app/core/janitor.py）。
    对话/用餐/总结这类**用户数据永不自动删** —— 这里也一并钉住。
    """
    import os
    import time as _time
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import app.config as cfg
    from app.core import janitor
    from app.core.db import HealthStore, to_utc_str

    media = tmp_path / "media"
    (media / "audio").mkdir(parents=True)
    old_media = media / "audio" / "tts-old.wav"
    new_media = media / "audio" / "tts-new.wav"
    old_media.write_bytes(b"x" * 64)
    new_media.write_bytes(b"x" * 64)
    stale = _time.time() - 3 * 86400
    os.utime(old_media, (stale, stale))

    monkeypatch.setattr(cfg, "PATHS", SimpleNamespace(media_dir=media, data_dir=tmp_path,
                                                     db_path=tmp_path / "e.db"))
    monkeypatch.setattr(cfg, "APP_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "CACHE_LOG_MAX_MB", 0.001)      # 强制触发轮转
    monkeypatch.setattr(cfg, "CACHE_MEDIA_KEEP_HOURS", 24.0)

    log = tmp_path / "server.log"
    log.write_text("\n".join(f"line{i}" for i in range(5000)) + "\n", encoding="utf-8")

    store = HealthStore(tmp_path / "e.db")
    store.init()
    with store.session() as conn:
        old_ts = to_utc_str(datetime.now(timezone.utc) - timedelta(days=200))
        for etype in ("emotion_detect", "dialogue"):          # 前者可清、后者是用户数据
            conn.execute("INSERT INTO events (timestamp, type, data_json, upload_status,"
                         " upload_attempts, last_error) VALUES (?,?,?,?,0,'')",
                         (old_ts, etype, "{}", "local"))
    monkeypatch.setattr("app.core.db.HealthStore", lambda *a, **k: store)

    result = janitor.cleanup_once(vacuum=False, verbose=False)
    assert result["media"] == 1 and not old_media.exists() and new_media.exists()
    assert result["logs_rotated"] == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) <= 400, "日志应只保留尾部"
    assert result["events"] == 1, "只该删过期的高频事件"
    with store.session() as conn:
        left = {r[0] for r in conn.execute("SELECT type FROM events")}
    assert left == {"dialogue"}, f"用户数据不能被自动删：{left}"


# ---------------- TTS 缓存（固定引导语免重复合成） ----------------
def test_tts_retries_once_on_rate_limit(monkeypatch):
    """429 限流要退避重试一次：一轮回复并发合成几句话很容易撞上限流，
    不重试就会连续记两次失败 → 熔断 120 秒 → "回复念到一半没声了"。
    """
    from app.voice import qwen_tts_engine as eng

    class Resp:
        def __init__(self, code, payload=None, content=b""):
            self.status_code, self._p, self.content, self.text = code, payload or {}, content, ""

        def json(self):
            return self._p

        def raise_for_status(self):
            pass

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:                      # 第一次撞限流
            return Resp(429, {"code": "Throttling.RateQuota", "message": "rate limit"})
        return Resp(200, {"output": {"audio": {"url": "https://x/y.wav", "data": ""}}})

    monkeypatch.setattr(eng.requests, "post", fake_post)
    monkeypatch.setattr(eng.requests, "get",
                        lambda url, timeout=None, **kw: Resp(200, content=b"RIFF" + b"\x00" * 600))
    monkeypatch.setattr(eng, "RATE_LIMIT_RETRY_SLEEP", 0.01)
    eng.reset_breaker()

    out = Path(tempfile.mkdtemp()) / "retry.wav"
    assert eng.synthesize("慢慢吃。", out) is True
    assert calls["n"] == 2, "撞限流后必须重试一次"
    assert eng.status()["consecutive_failures"] == 0, "重试成功后不该记失败"


def test_qwen3_thinking_is_off_for_voice_assistant():
    """Qwen3 系默认"先思考再回答"：实测首字 16.7s，关掉后 0.9s。

    语音助手必须关；但别的厂商（GLM 等）不认这个参数，只在 qwen3 系模型上带。
    """
    from app.core.ai_client import OpenAICompatibleClient

    qwen = OpenAICompatibleClient({"api_url": "https://x/v1", "model": "qwen3.7-flash",
                                   "api_key": "sk-test"})
    assert qwen._thinking_params() == {"enable_thinking": False}
    qwen_on = OpenAICompatibleClient({"api_url": "https://x/v1", "model": "qwen3.7-flash",
                                      "api_key": "sk-test", "enable_thinking": True})
    assert qwen_on._thinking_params() == {"enable_thinking": True}
    glm = OpenAICompatibleClient({"api_url": "https://x/v1", "model": "glm-4-flash",
                                  "api_key": "sk-test"})
    assert glm._thinking_params() == {}, "非 qwen3 系不能带这个参数（会被拒）"


def test_tts_sanitizes_unreadable_chars_without_tripping_breaker():
    """表情符号这类念不出来的字符必须先清掉，而且不能把发声熔断掉。

    真事故：AI 回复里常带 😊😋，切句后可能剩一个纯表情片段（如「😋」），
    直接发给 Qwen3-TTS 返回 HTTP 400 InvalidParameter。连续两次这种 400 就会
    触发熔断 —— 于是"回复念到一半没声了，而且之后两分钟整机都不出声"。
    """
    from app.voice import qwen_tts_engine as eng

    assert eng.sanitize_for_tts("😋") == ""
    assert eng.sanitize_for_tts("☆") == ""
    assert eng.sanitize_for_tts("   ") == ""
    assert eng.sanitize_for_tts("*") == ""
    assert eng.sanitize_for_tts("你好😊，慢慢吃。") == "你好，慢慢吃。"
    assert eng.sanitize_for_tts("已经吃完啦！👍棒") == "已经吃完啦！棒"

    # 念不出来的文本连接口都不用打（直接 False），且不累计失败、不熔断
    eng.reset_breaker()
    out = Path(tempfile.mkdtemp()) / "x.wav"
    for text in ("😋", "☆", "*"):
        assert eng.synthesize(text, out) is False
    st = eng.status()
    assert st["consecutive_failures"] == 0, "输入问题不该算进熔断计数"
    assert st["blocked_sec"] == 0, "输入问题不该让整机静音两分钟"


def test_tts_cache_roundtrip():
    from app.voice import tts_cache

    assert tts_cache.get("缓存测试句子", "zh-CN-XiaoxiaoNeural") is None
    tts_cache.put("缓存测试句子", "zh-CN-XiaoxiaoNeural", b"fake-mp3-bytes")
    got = tts_cache.get("缓存测试句子", "zh-CN-XiaoxiaoNeural")
    assert got == b"fake-mp3-bytes"
    # 不同音色不共享缓存
    assert tts_cache.get("缓存测试句子", "zh-CN-YunxiNeural") is None


def test_state_machine_prompts_are_cacheable():
    """状态机引导语是固定文案，应能被预合成（数量与 PROMPTS 一致）。"""
    from app.core.state_machine import PROMPTS

    assert len(PROMPTS) == 14
    assert all(isinstance(v, str) and v for v in PROMPTS.values())


def test_render_returns_content_height_and_landscape():
    """高清横屏 640x480：render 返回 (图, 内容高)，详细页内容高 > 480 可滚动。"""
    from app.config import SCREEN_H, SCREEN_W
    from app.display import display_state
    from app.display import render as R

    assert (SCREEN_W, SCREEN_H) == (640, 480), "必须是高清横屏 640x480"
    for page in ("face", "camera", "stats"):
        display_state.set_page(page)
        img, content_h = R.render(display_state, t=1.0)
        assert img.size == (SCREEN_W, SCREEN_H), f"{page} 尺寸异常: {img.size}"
        assert content_h >= SCREEN_H
        if page != "face":
            assert content_h > SCREEN_H, f"{page} 内容应超出一屏（可滚动）: {content_h}"
    display_state.set_page("face")


def test_scroll_clamped_and_state_tracked():
    from app.display.state import DisplayState

    st = DisplayState()
    st.set_content_height("stats", 1000.0)
    assert st.add_scroll("stats", 9999) == 1000.0 - 480.0   # 收敛到最大滚动
    assert st.add_scroll("stats", -9999) == 0.0             # 不为负
    assert st.get_scroll("face") == 0.0


def test_face_state_machine_transitions():
    """表情随语音流程切换：倾听 → 思考 → 说话 → 待机。"""
    from app.display.service import ScreenService
    from app.display.state import DisplayState

    svc = ScreenService(state=DisplayState())
    svc.on_listening()
    assert svc.state.face_state() == "listening"
    svc.on_thinking()
    assert svc.state.face_state() == "thinking"
    svc.on_speaking(est_seconds=1.0)
    assert svc.state.face_state() == "speaking"
    svc.on_idle()
    assert svc.state.face_state() == "idle"


def test_emotion_override_skips_neutral():
    """中性情感不覆盖待机表情（否则会顶掉可爱轮换）。"""
    from app.display.state import DisplayState

    st = DisplayState()
    st.set_emotion("neutral")
    assert st.face_state() == "idle"          # 仍是待机（由轮换决定具体表情）
    st.set_emotion("happy")
    assert st.face_state() == "relaxed"       # happy 映射为「放松」
    st.set_emotion("anxiety")
    assert st.face_state() == "anxiety"


def test_reply_emotion_does_not_overwrite_metrics():
    """设备屏第一页/第二页的情绪都只来自感知：AI 回复情感只切表情，不动 metrics。"""
    from app.display.service import ScreenService
    from app.display.state import DisplayState

    st = DisplayState()
    svc = ScreenService(state=st)
    st.set_metrics(emotion="放松", emotion_key="relaxed", vision=True)
    svc.on_emotion("angry")
    assert st.face_state() == "irritated"        # 表情切到生气的映射
    assert st.metrics()["emotion"] == "放松"      # 指标仍是感知结果


def test_interaction_settings_roundtrip(tmp_path):
    """模式切换带回默认互动级别；亮度/音量按范围夹取并落盘。"""
    from app.core.interaction_config import (AUTONOMY_OFF, MODE_CHAT, MODE_MEAL,
                                             AgentSettingsStore)

    path = tmp_path / "agent_settings.json"
    store = AgentSettingsStore(path)
    assert store.load().mode == MODE_MEAL

    s = store.save(mode=MODE_CHAT)
    assert s.mode == MODE_CHAT and s.autonomy == AUTONOMY_OFF
    assert s.vision_enabled is False             # 日常聊天模式关摄像头

    s = store.save(brightness=5, volume=999)
    assert (s.brightness, s.volume) == (20, 100)  # 夹到配置范围
    s = store.save(speech_rate=999)
    assert s.speech_rate == 50                    # AI 语速同样夹取

    s = store.save(mode=MODE_MEAL)
    assert s.vision_enabled is True               # 进食检测模式开摄像头
    assert s.brightness == 20                     # 其它字段保留
    assert AgentSettingsStore(path).load().mode == MODE_MEAL


def test_speech_rate_str_format(monkeypatch):
    """AI 语速转成百分比字符串（带符号）；Qwen3-TTS 在本地按此重采样时间轴。"""
    from app.core import interaction_config as ic

    monkeypatch.setattr(ic, "get_settings", lambda: ic.AgentSettings(speech_rate=15))
    assert ic.speech_rate_str() == "+15%"
    monkeypatch.setattr(ic, "get_settings", lambda: ic.AgentSettings(speech_rate=-10))
    assert ic.speech_rate_str() == "-10%"
    monkeypatch.setattr(ic, "get_settings", lambda: ic.AgentSettings(speech_rate=0))
    assert ic.speech_rate_str() == "+0%"


def test_agent_settings_api_accepts_all_panel_fields(tmp_path, monkeypatch):
    """/api/agent/settings 必须能保存面板上的每个字段（曾漏掉 AI 语速）。"""
    import asyncio

    import app.api.agent as agent_api
    from app.core import interaction_config as ic

    monkeypatch.setattr(ic, "agent_settings_store", ic.AgentSettingsStore(tmp_path / "s.json"))
    monkeypatch.setattr(agent_api, "apply_settings", lambda settings=None: None)

    class FakeRequest:
        async def json(self):
            return {"speech_rate": -20, "volume": 40, "brightness": 60}

    out = asyncio.run(agent_api.set_settings(FakeRequest()))
    assert out["settings"]["speech_rate"] == -20
    assert out["settings"]["volume"] == 40
    assert out["settings"]["brightness"] == 60
    assert ic.get_settings().speech_rate == -20
    assert ic.panel_payload()["speech_rate"]["min"] == -50


def test_model_probe_summary_flags_missing_models():
    """模型探针：没有输出时要给出可判读的排查结论。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "model_probe.py"
    spec = importlib.util.spec_from_file_location("model_probe", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    empty = probe.summarize([{"face_count": 0, "chews_per_min": 0, "bites_per_min": 0,
                              "emotion": ""}])
    assert empty["face_samples"] == 0
    assert any("人脸" in i for i in empty["issues"])
    assert any("咀嚼" in i for i in empty["issues"])
    assert any("送食" in i for i in empty["issues"])

    good = probe.summarize([{"face_count": 1, "chews_per_min": 22, "bites_per_min": 6,
                             "emotion": "平静", "perception_chewing": True,
                             "near_mouth": True, "mouth_dist_ratio": 0.8}])
    assert good["issues"] == []
    assert good["emotion_distribution"]["平静"] == 1
    assert good["near_mouth_samples"] == 1


def test_llm_prompt_drops_local_script():
    """提示词不含本地脚本；普通聊天不硬塞用餐阶段；带上一轮回答要求换说法。"""
    from app.api.dialogue import _build_llm_prompt

    prompt = _build_llm_prompt("我有点饿", "pre_meal")
    assert "状态机引导语" not in prompt
    assert "我有点饿" in prompt
    assert "餐前阶段" not in prompt              # 没开餐就不注入用餐阶段

    meal = _build_llm_prompt("我吃饱了", "mid_meal", meal_active=True,
                             last_reply="慢慢来，先深呼吸。")
    assert "餐中" in meal                        # 真的在用餐时才带阶段
    assert "不要重复同样的开头或句式" in meal      # 明确要求换一种说法


def test_ai_opening_requires_real_ai(monkeypatch):
    """未配置真实 AI 时，开场白返回明确告知，而不是本地规则话术。"""
    import app.api.dialogue as dlg
    from app.core.ai_client import LocalEchoClient

    monkeypatch.setattr(dlg, "get_ai_client", lambda: LocalEchoClient())
    assert dlg._ai_opening("pre_meal") == dlg.AI_UNCONFIGURED_NOTICE


def test_bite_tracker_counts_one_per_approach():
    """送食计数：进入嘴部范围记一次，贴着不重复计，离开并过冷却后才能再计。"""
    from app.vision.service import BiteTracker

    tracker = BiteTracker(min_near_sec=0.0, leave_sec=0.6,
                          refractory_sec=1.5, window_sec=30.0)
    assert tracker.update(True, 1.0) is True       # 第一次靠近 → 计一次
    assert tracker.update(True, 1.5) is False      # 还贴在嘴边 → 不重复
    assert tracker.update(False, 2.0) is False
    assert tracker.update(True, 2.2) is False      # 离开不足 0.6s → 不新建一次
    assert tracker.update(False, 3.0) is False
    assert tracker.update(False, 3.7) is False     # 期间累计离开 ≥0.6s
    assert tracker.update(True, 4.0) is True       # 再次靠近 → 计第二次
    assert tracker.bite_count == 2
    assert tracker.per_min() == 4.0                # 30 秒窗口：2 次 = 4 次/分


def test_dialogue_endpoint_accepts_user_message(monkeypatch):
    """非流式 /api/dialogue 带真实消息必须能回复（曾因多传 user_text 抛 500）。"""
    from fastapi.testclient import TestClient

    import app.api.dialogue as dlg
    from app.server import app

    class FakeClient:
        tts_voice = "zh-CN-XiaoxiaoNeural"

        # 刻意与真实 AI 客户端签名一致：不接受 user_text
        def chat(self, prompt, context=None, image_path=None, max_tokens=None):
            return "慢慢来，先做三次深呼吸。[EMOTION:happy]"

    monkeypatch.setattr(dlg, "get_ai_client", lambda: FakeClient())
    with TestClient(app) as client:
        resp = client.post("/api/dialogue", json={"message": "我有点饿",
                                                  "user_id": "pytest-msg"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["reply"].startswith("慢慢来")
    assert body["emotion"] == "happy"


def test_record_speech_rate(tmp_path):
    """每次说话都能算出语速并落库（"平均语速"不再永远不变）。"""
    from datetime import datetime, timedelta

    from app.core.db import HealthStore
    from app.voice.features import record_speech_rate

    store = HealthStore(tmp_path / "s.db")
    result = record_speech_rate("你好世界啊", 6.0, store)
    assert result["chars"] == 5
    assert result["speech_word_per_min"] == 50.0      # 5 字 / 0.1 分钟

    rows = store.query_event_range("wakeword", datetime.now() - timedelta(days=1),
                                   datetime.now() + timedelta(days=1))
    assert len(rows) == 1 and rows[0]["speech_word_per_min"] == 50.0


def test_emotion_distribution_merges_camera_and_speech(tmp_path):
    """情绪分布同时统计摄像头情绪与语速倾向，平均语速取自 wakeword 事件。"""
    from app.core.analytics import HealthDataQuery, WeekReportGenerator
    from app.core.db import HealthStore

    store = HealthStore(tmp_path / "a.db")
    store.add_event("emotion_detect", {"emotion": "sad", "emotion_label": "难过",
                                       "emotion_data": {"face_list": [
                                           {"emotion": "sad", "emotion_label": "难过"}]}},
                    status="local")
    store.add_event("emotion_detect", {"emotion": "happy", "emotion_label": "开心"},
                    status="local")
    store.add_event("wakeword", {"speech_word_per_min": 180.0}, status="local")
    store.add_event("wakeword", {"speech_word_per_min": 120.0}, status="local")

    gen = WeekReportGenerator(HealthDataQuery(store))
    dist = gen.calc_emotion_distribution()
    assert dist["难过"] == 1 and dist["开心"] == 1            # 摄像头
    assert dist["焦虑倾向"] == 1 and dist["平静"] == 1          # 语速
    assert gen.calc_avg_speech() == 150.0
    # 没有任何用餐记录时，情绪性进食占比只能是 0（原实现拿情绪采样条数除以
    # max(用餐数,1)，会凭空造出一个 >1 的假比例，曾出现 712.5% 这种结果）
    assert gen.calc_emotional_eat_ratio() == 0.0


def test_emotional_eat_ratio_is_a_real_ratio(tmp_path):
    """情绪性进食占比 = 负面情绪下的用餐次数 / 总用餐次数，必须落在 0~1。

    这个 bug 真出现过：分子是摄像头每 20 秒一条的**情绪采样**、分母是"一顿饭"，
    两者单位不同却直接相除，28 天里 57 条负面采样 ÷ 8 顿饭 = 712.5%。
    正确做法是按时间对齐：用餐区间 [started_ts, trigger_ts] 内出现过负面情绪才算。
    """
    from app.core.analytics import HealthDataQuery, WeekReportGenerator
    from app.core.db import HealthStore

    store = HealthStore(tmp_path / "r.db")
    base = 1_800_000_000.0          # 固定时间轴，避免依赖"现在"

    # 第 1 顿：区间内有负面情绪 → 算情绪性进食
    store.add_event("food_residual", {"started_ts": base, "trigger_ts": base + 600},
                    status="local")
    store.add_event("emotion_detect", {"emotion": "sad", "emotion_label": "难过",
                                       "trigger_ts": base + 300}, status="local")
    # 第 2 顿：区间内只有正面情绪 → 不算
    store.add_event("food_residual", {"started_ts": base + 3600, "trigger_ts": base + 4200},
                    status="local")
    store.add_event("emotion_detect", {"emotion": "happy", "emotion_label": "开心",
                                       "trigger_ts": base + 3900}, status="local")
    # 第 3 顿：区间内没有任何情绪采样 → 不算
    store.add_event("food_residual", {"started_ts": base + 7200, "trigger_ts": base + 7800},
                    status="local")
    # 大量负面采样，但都在用餐区间之外 → 不得把它们算进来
    for k in range(20):
        store.add_event("emotion_detect", {"emotion": "angry", "emotion_label": "愤怒",
                                           "trigger_ts": base + 100000 + k}, status="local")

    gen = WeekReportGenerator(HealthDataQuery(store))
    assert len(gen.meals) == 3
    ratio = gen.calc_emotional_eat_ratio()
    assert 0.0 <= ratio <= 1.0, f"占比必须是真比例，实际 {ratio}"
    assert ratio == round(1 / 3, 3), f"3 顿里 1 顿情绪性进食，期望 0.333，实际 {ratio}"


def test_metrics_carry_cumulative_chew_count(monkeypatch):
    """累计咀嚼次数要进入设备屏指标，前端两页都会显示"累计 N 次"。"""
    import app.vision.service as vs
    from app.display.service import ScreenService
    from app.display.state import DisplayState

    monkeypatch.setattr(vs.vision_service, "snapshot", lambda: {"analysis": {
        "chewing": {"chews_per_min": 24.0, "chew_level": "normal", "chew_count": 137},
        "eating_speed": {"bites_per_min": 6.0, "level": "normal"},
        "perception": {"emotion": {"face_count": 1, "state_zh": "放松",
                                   "emotion": "relaxed"}},
        "motion": {"active": False},
    }})
    monkeypatch.setattr(vs.vision_service, "is_running", lambda: True)

    state = DisplayState()
    ScreenService(state=state)._refresh_metrics()
    metrics = state.metrics()
    assert metrics["chew_count"] == 137        # 累计次数
    assert metrics["chews_per_min"] == 24.0    # 每分钟频率（原有）
    assert metrics["emotion"] == "放松"


def test_said_finished_handles_negation():
    """用户说"吃完了"算结束，"还没吃完"不能算。"""
    from app.core.state_machine import said_finished

    assert said_finished("我吃完了")
    assert said_finished("已吃完，谢谢")
    assert said_finished("不吃了")
    assert not said_finished("我还没吃完")
    assert not said_finished("没有吃完呢")
    assert not said_finished("还想再吃点")


def test_meal_asks_when_eating_stops():
    """餐中连续两分钟没在咀嚼 → 建议问一句"是不是吃完了"（**只有这一处**计时）。

    以前 autonomy 里另有一套同样的计时与冷却，两边各自数秒、各自计时，
    结果是"停一会儿被问一次、隔几分钟又被问一次"的反复追问。
    现在暂停判定只在 meal_manager.note_metrics 里做，autonomy 只负责开口。
    """
    import time as _time

    from app.core import interaction_config as ic
    from app.core.autonomy import AutonomyService
    from app.core.meal import MealPhase, meal_manager

    sustain = float(ic.EATING_IDLE["sustain_sec"])
    uid = "pytest-meal-idle-ask"
    idle = {"vision": True, "face_count": 1, "chews_per_min": 0.0}
    meal_manager.reset(uid)
    meal_manager.start(uid)
    meal_manager.to_mid(uid)                      # 这一餐正在吃
    try:
        assert meal_manager.note_metrics(uid, idle) is None      # 第一次只是开始计时
        assert meal_manager.current(uid).idle_since is not None

        # 还没到 sustain_sec → 不开口
        meal_manager.current(uid).idle_since = _time.time() - (sustain - 10)
        assert meal_manager.note_metrics(uid, idle) is None

        # 够了 → 建议问一句
        meal_manager.current(uid).idle_since = _time.time() - (sustain + 1)
        assert meal_manager.note_metrics(uid, idle) == "ask_finished"

        # 冷却期内不再追问（这是"别反复问"的保证）
        meal_manager.current(uid).idle_since = _time.time() - (sustain + 1)
        assert meal_manager.note_metrics(uid, idle) is None

        # 重新开始咀嚼 → 计时清零；冷却期过后可以再问
        meal_manager.note_metrics(uid, {"vision": True, "face_count": 1, "chews_per_min": 20.0})
        assert meal_manager.current(uid).idle_since is None
        meal_manager.current(uid).idle_asked_at = 0.0
        meal_manager.current(uid).idle_since = _time.time() - (sustain + 1)
        assert meal_manager.note_metrics(uid, idle) == "ask_finished"

        # 摄像头关着 / 桌边没人 → 不问（人都不在画面里，谈"吃完了没"没意义）
        meal_manager.reset(uid)
        meal_manager.start(uid)
        meal_manager.to_mid(uid)
        assert meal_manager.note_metrics(uid, {"vision": False, "face_count": 1,
                                              "chews_per_min": 0.0}) is None
        assert meal_manager.note_metrics(uid, {"vision": True, "face_count": 0,
                                              "chews_per_min": 0.0}) is None

        # autonomy 不再自己数秒：同样的"停下来"指标，没有这一餐的建议就不开口
        svc = AutonomyService()
        svc._last_spoke = _time.time()             # 排除"定期关心"那条规则
        assert svc._decide(ic.AUTONOMY_LEVELS[ic.AUTONOMY_NORMAL], idle, _time.time()) == ""

        # 不在用餐（没人说开饭、摄像头也没看到吃）→ 绝不问
        meal_manager.reset(uid)
        assert meal_manager.note_metrics(uid, idle) is None
        assert svc._decide(ic.AUTONOMY_LEVELS[ic.AUTONOMY_NORMAL], idle, _time.time()) == ""
    finally:
        meal_manager.reset(uid)


def test_meal_record_contains_meal_context(tmp_path, monkeypatch):
    """用户说吃完 → 记一条进食事件，并带上这一餐的咀嚼/情绪/对话信息。"""
    import time as _time
    from datetime import datetime, timedelta

    import app.api.dialogue as dlg
    from app.core.db import HealthStore

    monkeypatch.setattr(dlg, "_screen_metrics", lambda: {
        "chew_count": 137, "chews_per_min": 22.0, "chew_level": "normal",
        "bites_per_min": 6.0, "eat_level": "normal",
        "emotion": "放松", "face_count": 1})

    class FakeSession:
        meal_started_at = _time.time() - 600          # 10 分钟前开餐

    store = HealthStore(tmp_path / "meal.db")
    dlg._log_meal_record(store, "default", session=FakeSession(),
                         turns=[{"user_text": "我吃完了", "assistant_reply": "好呀"}],
                         phase="post_meal")

    rows = store.query_event_range("food_residual", datetime.now() - timedelta(days=1),
                                   datetime.now() + timedelta(days=1))
    assert len(rows) == 1
    rec = rows[0]
    assert rec["meal_finished"] is True
    assert rec["chew_count"] == 137 and rec["chews_per_min"] == 22.0
    assert rec["emotion"] == "放松" and rec["face_count"] == 1
    assert rec["duration_min"] == 10.0
    assert rec["turns"][0]["user"] == "我吃完了"


def test_confirmed_by_answer_needs_pending_question(monkeypatch):
    """只有"刚问过是不是吃完了"时，"对/嗯"才算吃完，避免误判普通对话。"""
    import time as _time

    import app.api.dialogue as dlg
    from app.core.autonomy import autonomy_service

    monkeypatch.setattr(autonomy_service, "_last_reason", "", raising=False)
    monkeypatch.setattr(autonomy_service, "_idle_asked_at", 0.0, raising=False)
    assert not dlg._confirmed_by_answer("对")          # 没问过 → 不算

    autonomy_service._last_reason = "finished_check"
    autonomy_service._idle_asked_at = _time.time()
    assert dlg._confirmed_by_answer("对")
    assert dlg._confirmed_by_answer("嗯，吃完了")
    assert not dlg._confirmed_by_answer("还没吃完")     # 否定
    assert not dlg._confirmed_by_answer("对，我还想再吃点东西")   # 太长，不算简单确认

    autonomy_service.clear_finished_question()
    assert not dlg._confirmed_by_answer("对")          # 记过一次后不再重复


def test_autonomy_decide_rules():
    """自主互动：关闭级别不主动；情绪（稳定两拍才触发）/速度触发；无信号按间隔关心。"""
    from app.core import interaction_config as ic
    from app.core.autonomy import AutonomyService
    from app.core.meal import meal_manager

    svc = AutonomyService()
    normal = ic.AUTONOMY_LEVELS[ic.AUTONOMY_NORMAL]
    off = ic.AUTONOMY_LEVELS[ic.AUTONOMY_OFF]
    meal_manager.reset("default")
    meal_manager.start("default")
    meal_manager.to_mid("default")            # 进食相关的触发只在餐中生效

    try:
        # 关闭级别不主动（用独立实例，避免影响下面情绪去抖的计数）
        assert AutonomyService()._decide(off, {"emotion_key": "sad"}, 999.0) == ""
        svc._last_spoke = 999.0        # 刚说过话，先排除"定期关心"那条规则的干扰
        # 情绪要连续两拍都读到才算数（单帧误判很常见）
        assert svc._decide(normal, {"emotion_key": "sad"}, 999.0) == ""
        assert svc._decide(normal, {"emotion_key": "sad"}, 1001.0) == "comfort"
        # 同一种情绪不重复安慰；换一种情绪同样要稳定两拍
        assert svc._decide(normal, {"emotion_key": "sad"}, 1003.0) == ""
        assert svc._decide(normal, {"emotion_key": "happy"}, 1005.0) == ""
        assert svc._decide(normal, {"emotion_key": "happy"}, 1007.0) == "celebrate"
        assert svc._decide(normal, {"chew_level": "fast"}, 1009.0) == "chew_fast"
        assert svc._decide(normal, {"eat_level": "fast"}, 1011.0) == "eat_fast"

        svc._last_spoke = 900.0
        assert svc._decide(normal, {}, 999.0) == ""                 # 还没到关心间隔
        assert svc._decide(normal, {}, 900.0 + normal["checkin_gap"]) == "checkin"

        # 不在用餐时，任何进食相关的触发都不开口（"慢点嚼""吃完了吗"都没道理）
        meal_manager.reset("default")
        assert svc._decide(normal, {"chew_level": "fast"}, 9999.0) == ""
        assert svc._decide(normal, {"eat_level": "fast"}, 9999.0) == ""
        assert AutonomyService()._decide(
            normal, {"vision": True, "face_count": 1, "chews_per_min": 0.0}, 9999.0) == ""
    finally:
        meal_manager.reset("default")


def test_autonomy_publishes_proactive_event(monkeypatch):
    """自主互动触发后直接产出「文本 + 表情 + 语音」事件，不需要用户先说话。"""
    import base64

    from app.core.autonomy import AutonomyService

    class FakeClient:
        tts_voice = "zh-CN-XiaoxiaoNeural"

        def chat(self, prompt, context=None, **kwargs):
            return "别难过，我陪着你，先做三次深呼吸。"

    monkeypatch.setattr("app.core.ai_client.get_ai_client", lambda: FakeClient())
    monkeypatch.setattr("app.voice.tts_cache.get", lambda text, voice: b"fake-mp3")

    svc = AutonomyService()
    q = svc.subscribe()
    svc._speak("comfort")
    event = q.get_nowait()

    assert event["reason"] == "comfort"
    assert event["text"].startswith("别难过")
    assert event["emotion"] == "love"          # “别难过”命中安抚关键词
    assert base64.b64decode(event["audio_b64"]) == b"fake-mp3"
    svc.unsubscribe(q)


def test_turn_history_keeps_last_two():
    from app.display.state import DisplayState

    st = DisplayState()
    for i in range(5):
        st.add_turn(f"用户{i}", f"回复{i}")
    turns = st.turns()
    assert len(turns) == 2
    assert turns[-1].user == "用户4" and turns[0].user == "用户3"


def test_page_switch_wraps():
    from app.display.state import PAGES, DisplayState

    st = DisplayState()
    seen = [st.get_page()]
    for _ in range(len(PAGES)):
        seen.append(st.next_page())
    assert seen[-1] == seen[0]                # 循环回首页
    assert set(seen[:-1]) == set(PAGES)


def test_emotion_label_localized():
    from app.display.render import emotion_label

    assert emotion_label("relaxed") == "放松"
    assert emotion_label("anticipation") == "期待"
    assert emotion_label("aggrieved") == "委屈"
    assert emotion_label("happy") == "放松"      # 旧键也有中文
    assert emotion_label("开心") == "开心"        # 已是中文则透传
    assert emotion_label(None) == "—"
