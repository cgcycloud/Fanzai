"""定期清理"会自己长胖"的文件 —— 缓存、临时音频、日志、高频事件表。

为什么要它：设备是**长期无人值守**跑的（树莓派按天、按周开机），
下面这些东西会一直涨：

  * `data_local/media/audio/` 下每句 TTS 都会落一个临时 wav（正常用完就删，
    但合成失败/进程被杀会留下残file），还有预热/应答的中间文件；
  * 根目录与 `data_local/` 下的 `*.log`：一个下午就能到几十万行（实测 10066 行）；
  * 数据库 `events` 表：情绪采样每约 20 秒一条 → 一天 4000+ 条，
    而周报/分析只用最近几天到 90 天，更老的纯占体积。

清理策略（都可在 `app/config.py` 调）：

  * 媒体临时文件：超过 `CACHE_MEDIA_KEEP_HOURS`（默认 24h）就删；
  * 日志：超过 `CACHE_LOG_MAX_MB`（默认 2MB）就**保留尾部**重写（不删整个日志，
    最近的问题现场还在）；`CACHE_LOG_KEEP_DAYS` 天没写过的日志文件直接删掉；
  * 事件表：只清高频低价值类型（emotion_detect / emotion / wakeword），
    保留 `CACHE_EVENT_KEEP_DAYS`（默认 90 天）；对话、用餐、总结记录**永不自动删**；
    清理后顺带 `VACUUM` 收空间（低频，一天最多一次）。

线程在 `server.py` 启动时拉起（daemon，失败不影响服务）；也可以手动跑：

    .venv/Scripts/python -m app.main cleanup
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List

from .. import config

# 只清这几类高频事件；别的类型（dialogue/food_residual/meal_summary/emotion 之外的）
# 是用户数据，不做自动删除。
PRUNABLE_EVENTS = ("emotion_detect", "wakeword")


def _media_cache_files(media_dir: Path, keep_hours: float) -> List[Path]:
    """媒体目录里过期的临时音频/图片（含 `_` 前缀的诊断产物）。"""
    if not media_dir.exists():
        return []
    cut = time.time() - keep_hours * 3600.0
    out: List[Path] = []
    for p in media_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".wav", ".mp3", ".ogg", ".m4a", ".jpg", ".png"):
            continue
        if p.stat().st_mtime >= cut:
            continue
        out.append(p)
    return out


def _rotate_log(path: Path, max_mb: float, keep_tail_lines: int = 400) -> bool:
    """日志超限时只保留尾部若干行（保留现场，又不会无限涨）。"""
    try:
        if not path.is_file() or path.stat().st_size <= max_mb * 1024 * 1024:
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        path.write_text("".join(lines[-keep_tail_lines:]), encoding="utf-8")
        return True
    except Exception:
        return False


def _log_files(roots: List[Path]) -> List[Path]:
    """两个目录下的 *.log（去重：data_dir 可能就在项目根里，别把同一个文件算两遍）。"""
    seen: set[Path] = set()
    out: List[Path] = []
    for root in roots:
        if root.exists():
            for p in root.glob("*.log"):
                if p.is_file():
                    key = p.resolve()
                    if key not in seen:
                        seen.add(key)
                        out.append(p)
    return out


def _prune_events(keep_days: float, vacuum: bool = False) -> int:
    """删掉过期的高频事件；返回删除条数。"""
    from .db import HealthStore, to_utc_str
    from datetime import datetime, timedelta, timezone

    store = HealthStore()
    cutoff = to_utc_str(datetime.now(timezone.utc) - timedelta(days=keep_days))
    removed = 0
    try:
        with store.session() as conn:
            for etype in PRUNABLE_EVENTS:
                cur = conn.execute("DELETE FROM events WHERE type=? AND timestamp < ?",
                                   (etype, cutoff))
                removed += cur.rowcount or 0
            if vacuum and removed:
                conn.execute("VACUUM")
    except Exception as exc:
        print(f"[janitor] 事件清理失败: {exc}", flush=True)
    return removed


def cleanup_once(*, vacuum: bool = False, verbose: bool = True) -> Dict[str, int]:
    """跑一轮清理，返回各项计数（供日志/接口展示）。"""
    from .. import config as cfg

    result = {"media": 0, "logs_rotated": 0, "logs_deleted": 0, "events": 0}
    for p in _media_cache_files(cfg.PATHS.media_dir, float(cfg.CACHE_MEDIA_KEEP_HOURS)):
        try:
            p.unlink()
            result["media"] += 1
        except Exception:
            pass

    log_cut = time.time() - float(cfg.CACHE_LOG_KEEP_DAYS) * 86400.0
    for p in _log_files([cfg.APP_ROOT, cfg.PATHS.data_dir]):
        try:
            if p.stat().st_mtime < log_cut:
                p.unlink()
                result["logs_deleted"] += 1
            elif _rotate_log(p, float(cfg.CACHE_LOG_MAX_MB)):
                result["logs_rotated"] += 1
        except Exception:
            pass

    result["events"] = _prune_events(float(cfg.CACHE_EVENT_KEEP_DAYS), vacuum=vacuum)
    if verbose:
        print(f"[janitor] 清理完成：媒体 {result['media']} 个 · 日志轮转 {result['logs_rotated']} / "
              f"删除 {result['logs_deleted']} · 过期事件 {result['events']} 条"
              f"（保留媒体 {cfg.CACHE_MEDIA_KEEP_HOURS}h、事件 {cfg.CACHE_EVENT_KEEP_DAYS} 天）",
              flush=True)
    return result


class CacheJanitor:
    """后台保洁线程（跟着服务起，周期性跑 cleanup_once）。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: Dict[str, object] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cache-janitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, object]:
        return {"running": bool(self._thread and self._thread.is_alive()),
                "interval_h": float(config.CACHE_CLEAN_INTERVAL_H),
                "last": dict(self._last)}

    def _loop(self) -> None:
        # 启动先等 60 秒（别和模型加载抢 IO），之后每 interval 小时跑一次
        if self._stop.wait(60.0):
            return
        while not self._stop.is_set():
            try:
                self._last = {"at": time.time(), **cleanup_once(vacuum=True)}
            except Exception as exc:
                print(f"[janitor] 清理异常: {exc}", flush=True)
            if self._stop.wait(max(600.0, float(config.CACHE_CLEAN_INTERVAL_H) * 3600.0)):
                return


cache_janitor = CacheJanitor()
