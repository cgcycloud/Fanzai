"""报告 API —— 简易周报 + 完整健康分析（4周周报/风险/复查/洞察）+ 个性文档。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from ..core.analytics import health_analyzer, run_full_health_analysis
from ..core.db import HealthStore, utc_now

router = APIRouter(prefix="/api")

# 体重趋势的内部枚举 → 给用户看的中文
_TREND_ZH = {"up": "上升", "down": "下降", "stable": "平稳"}


@router.get("/report")
async def weekly_report(user_id: str = "default", days: int = 7):
    """简易周报：对话数 / 用餐数 / 情绪分布。"""
    store = HealthStore()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with store.session() as con:
            dialogue_count = con.execute(
                "SELECT COUNT(*) FROM events WHERE type='dialogue' "
                "AND json_extract(data_json, '$.user_id') = ? AND timestamp >= ?",
                (user_id, cutoff)).fetchone()[0]
            meal_count = con.execute(
                "SELECT COUNT(*) FROM events WHERE type='meal_summary' "
                "AND json_extract(data_json, '$.user_id') = ? AND timestamp >= ?",
                (user_id, cutoff)).fetchone()[0]
            emotion_rows = con.execute(
                "SELECT data_json FROM events WHERE type='emotion' "
                "AND json_extract(data_json, '$.user_id') = ? AND timestamp >= ?",
                (user_id, cutoff)).fetchall()
        emotion_counts: dict[str, int] = {}
        for r in emotion_rows:
            emo = json.loads(r[0]).get("emotion", "unknown")
            emotion_counts[emo] = emotion_counts.get(emo, 0) + 1
        return {
            "user_id": user_id, "period_days": days,
            "total_dialogues": dialogue_count, "total_meal_sessions": meal_count,
            "emotion_distribution": emotion_counts, "generated_at": utc_now(),
        }
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/analytics")
async def analytics(user_id: str = "default"):
    """完整健康分析：4周周报 + 风险预警 + 复查计划 + 个性化洞察。"""
    return health_analyzer(user_id)


@router.get("/personal-doc")
async def personal_doc(user_id: str = "default"):
    """个性化健康文档（Markdown）：个性文档 + 总结，供前端展示/下载。"""
    result = health_analyzer(user_id)
    wr = result.get("week_report", {})
    warnings = result.get("warnings", [])
    rp = result.get("review_plan", {})
    lines = [
        f"# 正念饭崽 · 个性化健康文档",
        "",
        f"**用户**: {user_id}  |  **生成时间**: {utc_now()}",
        "",
        "## 一、近 4 周饮食总览",
        f"- 用餐次数：{wr.get('meal_count', 0)} 次",
        f"- 平均用餐间隔：{wr.get('avg_meal_interval_h', 0)} 小时",
        f"- 情绪性进食占比：{round(float(wr.get('emotional_eat_ratio', 0)) * 100, 1)}%",
        f"- 近两周平均语速：{wr.get('avg_speech_speed', 0)} 字/分钟",
        # 直接给中文，别把内部枚举（up/down/stable）丢给用户看
        f"- 体重趋势：{_TREND_ZH.get(str(wr.get('weight_trend', 'stable')), '平稳')}",
        f"- 饮食依从性评分：{wr.get('adherence_score', 0)} / 100",
        "",
        "## 二、健康风险预警",
    ]
    if warnings:
        for w in warnings:
            lines.append(f"- **[{w.get('level', 'mid')}]** {w.get('desc', '')}  ")
            lines.append(f"  建议：{w.get('suggest', '')}")
    else:
        lines.append("- 暂无明显饮食、情绪、体重异常风险。")
    lines += [
        "",
        "## 三、复查计划",
        f"- 当前阶段：{rp.get('phase', '-')}",
        f"- 复查频率：{rp.get('frequency', '-')}",
        f"- 下次复查：{rp.get('next_review', '-')}",
        "",
        "## 四、个性化总结",
        result.get("personal_insight", "健康分析暂时不可用。"),
        "",
    ]
    return {"user_id": user_id, "markdown": "\n".join(lines)}


@router.post("/meal-summary")
async def meal_summary(request_body: dict):
    """一餐结束：记录 meal_summary 事件（给脚本/调试用；幂等统计仍在 /api/dialogue/meal/finish）。

    数据直接取 `meal_manager` 里这一餐的真实进度（时长/轮数），
    不再依赖按轮数推进的 `DialogueStateMachine`（那套的轮数跟吃没吃完无关）。
    """
    from ..core.meal import meal_manager

    body = request_body
    user_id = str(body.get("user_id") or "default")
    meal = meal_manager.current(user_id)
    data = {
        "user_id": user_id,
        "total_turns": meal.turns if meal else 0,
        "duration_min": meal.duration_min if meal else 0.0,
        "phase": meal.phase.value if meal else "idle",
    }
    HealthStore().add_event("meal_summary", data, status="local")
    return {"ok": True, "summary": data}
