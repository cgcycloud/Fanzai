"""健康分析：4 周周报 / 风险预警 / 复查计划 / 个性化洞察。"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .. import config
from .db import HealthStore

# 负面情绪标签：既认旧的中文标签，也认本地感知模型的英文键
NEGATIVE_EMOTIONS = {
    "愤怒", "悲伤", "恐惧", "厌恶", "难过", "焦虑", "烦躁", "委屈", "紧张",
    "angry", "sad", "fear", "disgust", "anxiety", "cry", "aggrieved",
    "hatred", "irritated", "nervous",
}


# ===================== 数据结构 =====================
@dataclass
class WeekReport:
    report_start: str
    report_end: str
    total_days: int
    meal_count: int
    avg_meal_interval_h: float
    emotional_eat_ratio: float
    avg_speech_speed: float
    weight_trend: str   # up/down/stable
    adherence_score: int  # 0-100
    emotion_distribution: Dict[str, int] = field(default_factory=dict)


@dataclass
class RiskWarning:
    warning_type: str
    level: str           # low/mid/high
    desc: str
    trigger_days: int
    suggest: str


@dataclass
class AllRiskSummary:
    emotion_warn: Optional[RiskWarning]
    weight_warn: Optional[RiskWarning]
    diet_risk: Optional[RiskWarning]
    warning_list: List[RiskWarning] = field(default_factory=list)


@dataclass
class ReviewPlan:
    current_phase: str
    phase_name: str
    review_interval_days: int
    next_review_date: str
    review_frequency_desc: str


@dataclass
class FullAnalysisOutput:
    week_report: WeekReport
    risk_summary: AllRiskSummary
    personal_insight: str
    review_plan: ReviewPlan


# ===================== 数据查询 =====================
class HealthDataQuery:
    def __init__(self, store: HealthStore):
        self.store = store

    def _range(self, event_type: str, days: int) -> List[Dict[str, Any]]:
        end = datetime.now()
        start = end - timedelta(days=days)
        return self.store.query_event_range(event_type, start, end)

    def get_meal_records_4w(self) -> List[Dict]:
        return self._range("food_residual", 28)

    def get_emotion_records_4w(self) -> List[Dict]:
        return self._range("emotion_detect", 28)

    def get_speech_speed_14d(self) -> List[Dict]:
        return self._range("wakeword", 14)

    def get_weight_records_90d(self) -> List[Dict]:
        return self._range("weight_record", 90)

    def get_diet_time_30d(self) -> List[Dict]:
        return self._range("vision_full_result", 30)


# ===================== 周报 =====================
class WeekReportGenerator:
    def __init__(self, query: HealthDataQuery):
        self.query = query
        self.meals = query.get_meal_records_4w()
        self.emotions = query.get_emotion_records_4w()
        self.speech_data = query.get_speech_speed_14d()
        self.weight_data = query.get_weight_records_90d()

    def calc_meal_count(self) -> int:
        return len(self.meals)

    def calc_avg_meal_interval(self) -> float:
        ts_list = sorted(m.get("trigger_ts", 0) for m in self.meals if m.get("trigger_ts"))
        if len(ts_list) < 2:
            return 0.0
        diff_sum = sum(ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list)))
        return round(diff_sum / (len(ts_list) - 1) / 3600, 2)

    def calc_emotional_eat_ratio(self) -> float:
        """情绪性进食占比 = **处于负面情绪的用餐次数 / 总用餐次数**（0~1）。

        原先的实现是 `负面情绪采样条数 / 用餐次数` —— 分子是摄像头每 20 秒左右
        落一条的情绪采样、分母是"一顿饭"，两者**单位不同、不可相除**，
        于是出现过 712.5% 这种荒谬结果（57 条情绪采样 ÷ 8 顿饭）。

        正确做法是按时间对齐：一顿饭的时间跨度是 [started_ts, trigger_ts]，
        只要这期间（前后各放宽 MARGIN 秒）出现过负面情绪，就记一次"情绪性进食"。
        没有用餐记录时返回 0（而不是除以 1 造出一个假比例）。
        """
        if not self.meals:
            return 0.0

        # 情绪采样按时间排序，用二分找落在用餐区间内的那些
        emo_ts = sorted(float(r["trigger_ts"]) for r in self.emotions
                        if r.get("trigger_ts") is not None)
        neg_ts = sorted(
            float(r["trigger_ts"]) for r in self.emotions
            if r.get("trigger_ts") is not None and self._is_negative(r)
        )
        if not emo_ts:
            return 0.0

        margin = 60.0        # 秒：用餐起止点前后各放宽一分钟
        emotional = 0
        for meal in self.meals:
            start = meal.get("started_ts")
            end = meal.get("trigger_ts")
            try:
                lo = float(start if start is not None else end) - margin
                hi = float(end) + margin
            except (TypeError, ValueError):
                continue
            i = bisect_left(neg_ts, lo)
            if i < len(neg_ts) and neg_ts[i] <= hi:
                emotional += 1
        return round(emotional / len(self.meals), 3)

    @staticmethod
    def _is_negative(rec: Dict[str, Any]) -> bool:
        """这条情绪采样是否属于负面情绪（兼容多种字段与嵌套的 face_list）。"""
        labels = {rec.get("emotion_label"), rec.get("emotion_zh"), rec.get("emotion")}
        face_list = (rec.get("emotion_data") or {}).get("face_list") or []
        for f in face_list:
            if isinstance(f, dict):
                labels.add(f.get("emotion_label"))
                labels.add(f.get("emotion_zh"))
                labels.add(f.get("emotion"))
        labels.discard(None)
        return bool(labels & NEGATIVE_EMOTIONS)

    def calc_emotion_distribution(self) -> Dict[str, int]:
        """情绪分布 = 摄像头识别的情绪 + 语速推断的情绪倾向，两者同步计入。

        摄像头数据来自 emotion_detect 事件（本地感知线程按变化落库），
        语速数据来自 wakeword 事件（每次说话都会记一条）。
        """
        dist: Dict[str, int] = {}
        for rec in self.emotions:
            label = rec.get("emotion_label") or rec.get("emotion_zh") or rec.get("emotion")
            if label:
                dist[label] = dist.get(label, 0) + 1
        for rec in self.speech_data:
            speed = rec.get("speech_word_per_min")
            if not speed:
                continue
            if speed >= config.SPEECH_FAST_THRESH:
                label = "焦虑倾向"
            elif speed <= config.SPEECH_SLOW_THRESH:
                label = "低落倾向"
            else:
                label = "平静"
            dist[label] = dist.get(label, 0) + 1
        return dist

    def calc_avg_speech(self) -> float:
        speeds = [r.get("speech_word_per_min") for r in self.speech_data if r.get("speech_word_per_min")]
        return round(sum(speeds) / len(speeds), 1) if speeds else 0.0

    def calc_weight_trend(self) -> str:
        if len(self.weight_data) < 2:
            return "stable"
        sorted_w = sorted(self.weight_data, key=lambda x: x.get("record_ts", 0))
        diff = sorted_w[-1].get("weight_kg", 0) - sorted_w[0].get("weight_kg", 0)
        if diff > 1.0:
            return "up"
        if diff < -1.0:
            return "down"
        return "stable"

    def calc_adherence_score(self) -> int:
        """饮食依从性评分 0-100。"""
        score = 0
        if self.calc_emotional_eat_ratio() < 0.3:
            score += 30
        long_window = night_eat = 0
        for d in self.query.get_diet_time_30d():
            if d.get("eating_window_h", 0) > config.EAT_WINDOW_MAX_HOUR:
                long_window += 1
            if d.get("has_night_meal", False):
                night_eat += 1
        if long_window < config.EAT_WINDOW_CONT_DAYS:
            score += 25
        if night_eat < 3:
            score += 25
        meal_days = len({int(m.get("trigger_ts", 0) // 86400) for m in self.meals if m.get("trigger_ts")})
        if meal_days >= 24:
            score += 20
        return min(score, 100)

    def generate(self) -> WeekReport:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=28)
        return WeekReport(
            report_start=start_dt.strftime("%Y-%m-%d"),
            report_end=end_dt.strftime("%Y-%m-%d"),
            total_days=28,
            meal_count=self.calc_meal_count(),
            avg_meal_interval_h=self.calc_avg_meal_interval(),
            emotional_eat_ratio=self.calc_emotional_eat_ratio(),
            avg_speech_speed=self.calc_avg_speech(),
            weight_trend=self.calc_weight_trend(),
            adherence_score=self.calc_adherence_score(),
            emotion_distribution=self.calc_emotion_distribution(),
        )


# ===================== 风险预警 =====================
class RiskWarningDetector:
    def __init__(self, query: HealthDataQuery):
        self.query = query
        self.warnings: List[RiskWarning] = []

    def check_emotion_speech_warn(self) -> Optional[RiskWarning]:
        """连续 N 天语速过快（焦虑倾向）/ 过慢（情绪低落）。"""
        daily_speed: Dict[str, List[float]] = {}
        for rec in self.query.get_speech_speed_14d():
            ts, spd = rec.get("trigger_ts", 0), rec.get("speech_word_per_min")
            if not ts or spd is None:
                continue
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            daily_speed.setdefault(day, []).append(spd)
        daily_avg = {d: sum(s) / len(s) for d, s in daily_speed.items()}

        continuous_fast = continuous_slow = 0
        fast_warn = slow_warn = None
        for d in sorted(daily_avg):
            s = daily_avg[d]
            if s >= config.SPEECH_FAST_THRESH:
                continuous_fast, continuous_slow = continuous_fast + 1, 0
            elif s <= config.SPEECH_SLOW_THRESH:
                continuous_slow, continuous_fast = continuous_slow + 1, 0
            else:
                continuous_fast = continuous_slow = 0
            if continuous_fast >= config.CONTINUOUS_WARN_DAYS and not fast_warn:
                fast_warn = RiskWarning(
                    warning_type="anxiety_tendency", level="mid",
                    desc=f"连续{continuous_fast}天语速超过{config.SPEECH_FAST_THRESH}字/分钟，存在焦虑倾向风险",
                    trigger_days=continuous_fast,
                    suggest="建议放缓语速、深呼吸放松，减少高压进食场景",
                )
            if continuous_slow >= config.CONTINUOUS_WARN_DAYS and not slow_warn:
                slow_warn = RiskWarning(
                    warning_type="depress_tendency", level="mid",
                    desc=f"连续{continuous_slow}天语速低于{config.SPEECH_SLOW_THRESH}字/分钟，存在情绪低落倾向",
                    trigger_days=continuous_slow,
                    suggest="多进行轻度运动，和家人沟通，避免独自压抑用餐",
                )
        for w in (fast_warn, slow_warn):
            if w:
                self.warnings.append(w)
                return w
        return None

    def check_weight_fluctuate_warn(self) -> Optional[RiskWarning]:
        """90 天体重波动超 5%。"""
        weight_rec = self.query.get_weight_records_90d()
        if len(weight_rec) < 2:
            return None
        sorted_w = sorted(weight_rec, key=lambda x: x.get("record_ts", 0))
        first_w = sorted_w[0].get("weight_kg", 0)
        last_w = sorted_w[-1].get("weight_kg", 0)
        if first_w == 0:
            return None
        change_ratio = abs(last_w - first_w) / first_w
        if change_ratio >= config.WEIGHT_CHANGE_RATIO:
            w = RiskWarning(
                warning_type="weight_abnormal", level="high",
                desc=f"近90天体重波动幅度{round(change_ratio * 100, 1)}%，"
                     f"超过{round(config.WEIGHT_CHANGE_RATIO * 100)}%安全阈值，存在代谢风险",
                trigger_days=config.WEIGHT_WINDOW_DAYS,
                suggest="固定每日晨起称重，记录三餐热量，持续波动需就医排查内分泌问题",
            )
            self.warnings.append(w)
            return w
        return None

    def check_diet_metabolic_risk(self) -> Optional[RiskWarning]:
        """进食窗口 >12h 连续 3 天 / 21 点后宵夜。"""
        daily_window: Dict[str, List[float]] = {}
        night_meal_days: set[str] = set()
        for rec in self.query.get_diet_time_30d():
            ts = rec.get("trigger_ts", 0)
            if not ts:
                continue
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            daily_window.setdefault(day, []).append(rec.get("eating_window_h", 0))
            if rec.get("has_night_meal", False):
                night_meal_days.add(day)

        continuous_long = trigger_days = 0
        for d in sorted(daily_window):
            if sum(daily_window[d]) / len(daily_window[d]) > config.EAT_WINDOW_MAX_HOUR:
                continuous_long += 1
            else:
                continuous_long = 0
            if continuous_long >= config.EAT_WINDOW_CONT_DAYS:
                trigger_days = continuous_long
        night_count = len(night_meal_days)

        if trigger_days >= config.EAT_WINDOW_CONT_DAYS or night_count >= config.NIGHT_EAT_WARN_DAYS:
            desc = ""
            if trigger_days >= config.EAT_WINDOW_CONT_DAYS:
                desc += f"连续{trigger_days}天每日进食窗口超过{config.EAT_WINDOW_MAX_HOUR}小时；"
            if night_count >= config.NIGHT_EAT_WARN_DAYS:
                desc += f"近30天有{night_count}次{config.NIGHT_EAT_HOUR}点后宵夜记录"
            w = RiskWarning(
                warning_type="metabolic_risk", level="mid",
                desc="进食时间不规律，易扰乱昼夜代谢节律，升高胰岛素抵抗风险：" + desc,
                trigger_days=max(trigger_days, night_count),
                suggest="固定早7-晚19点进食窗口，20点后不再摄入正餐，减少宵夜",
            )
            self.warnings.append(w)
            return w
        return None

    def run_all_check(self) -> AllRiskSummary:
        self.warnings.clear()
        emo_w = self.check_emotion_speech_warn()
        weight_w = self.check_weight_fluctuate_warn()
        diet_w = self.check_diet_metabolic_risk()
        return AllRiskSummary(
            emotion_warn=emo_w, weight_warn=weight_w, diet_risk=diet_w,
            warning_list=self.warnings,
        )


# ===================== 复查计划 =====================
class ReviewPlanCalculator:
    def __init__(self, start_ts: float):
        self.start_dt = datetime.fromtimestamp(start_ts)
        self.now = datetime.now()
        self.months_passed = (self.now.year - self.start_dt.year) * 12 + (self.now.month - self.start_dt.month)

    def get_phase_key(self) -> str:
        m = self.months_passed
        if 0 <= m <= 3:
            return "intensive"
        if 4 <= m <= 6:
            return "consolidate"
        return "maintain"

    def compute_plan(self) -> ReviewPlan:
        cfg = config.PHASE_CONFIG[self.get_phase_key()]
        interval = cfg["interval_days"]
        return ReviewPlan(
            current_phase=self.get_phase_key(),
            phase_name=cfg["name"],
            review_interval_days=interval,
            next_review_date=(self.now + timedelta(days=interval)).strftime("%Y-%m-%d"),
            review_frequency_desc=f"{cfg['name']}，每{interval}天复查一次",
        )


# ===================== 个性化洞察 =====================
# 体重趋势的内部枚举 → 给用户看的中文（别把 up/down/stable 直接丢给用户）
_TREND_ZH = {"up": "上升", "down": "下降", "stable": "平稳"}


def generate_personal_insight(report: WeekReport, risks: AllRiskSummary, review: ReviewPlan) -> str:
    lines = [
        f"近4周共用餐{report.meal_count}次，平均用餐间隔{report.avg_meal_interval_h}小时，"
        f"饮食依从性{report.adherence_score}分。",
        f"情绪性进食占比{round(report.emotional_eat_ratio * 100, 1)}%，"
        f"近两周平均语速{report.avg_speech_speed}字/分钟，"
        f"体重整体呈{_TREND_ZH.get(report.weight_trend, '平稳')}趋势。",
    ]
    if risks.warning_list:
        warn_text = "当前存在健康风险："
        for w in risks.warning_list:
            warn_text += w.desc + "；"
        lines.append(warn_text)
    else:
        lines.append("暂无明显饮食、情绪、体重异常风险。")
    lines.append(f"当前处于{review.phase_name}，下次复查时间{review.next_review_date}。")
    return "".join(lines)


def run_full_health_analysis(store: HealthStore, manage_start_ts: float) -> FullAnalysisOutput:
    query = HealthDataQuery(store)
    week_report = WeekReportGenerator(query).generate()
    risk_summary = RiskWarningDetector(query).run_all_check()
    review_plan = ReviewPlanCalculator(manage_start_ts).compute_plan()
    insight = generate_personal_insight(week_report, risk_summary, review_plan)
    return FullAnalysisOutput(week_report, risk_summary, insight, review_plan)


def health_analyzer(user_id: str = "default") -> Dict[str, Any]:
    """供对话/前端调用的健康分析总入口。"""
    try:
        store = HealthStore()
        result = run_full_health_analysis(store, datetime.now().timestamp())
        return {
            "user_id": user_id,
            "week_report": {
                "report_start": result.week_report.report_start,
                "report_end": result.week_report.report_end,
                "total_days": result.week_report.total_days,
                "meal_count": result.week_report.meal_count,
                "avg_meal_interval_h": result.week_report.avg_meal_interval_h,
                "emotional_eat_ratio": result.week_report.emotional_eat_ratio,
                "avg_speech_speed": result.week_report.avg_speech_speed,
                "weight_trend": result.week_report.weight_trend,
                "adherence_score": result.week_report.adherence_score,
                # 摄像头情绪 + 语速倾向合并后的分布（设备屏"健康数据"页直接显示）
                "emotion_distribution": result.week_report.emotion_distribution,
            },
            "warnings": [
                {"type": w.warning_type, "level": w.level, "desc": w.desc, "suggest": w.suggest}
                for w in result.risk_summary.warning_list
            ],
            "personal_insight": result.personal_insight,
            "review_plan": {
                "phase": result.review_plan.phase_name,
                # 同时给出两种字段名，避免前端/旧客户端读取到 undefined
                "next_review": result.review_plan.next_review_date,
                "next_review_date": result.review_plan.next_review_date,
                "frequency": result.review_plan.review_frequency_desc,
                "interval_days": result.review_plan.review_interval_days,
            },
        }
    except Exception as e:
        return {"user_id": user_id, "error": str(e),
                "personal_insight": "健康分析暂时不可用，请稍后再试。"}
