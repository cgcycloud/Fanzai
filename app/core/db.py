"""SQLite 存储层：events（行为/健康事件）+ media（媒体文件登记）。

事件类型约定（与 analytics / 前端报告强关联，勿随意改名）：
  dialogue            对话记录（user_text / assistant_reply / phase / user_id）
  meal_summary        一餐总结（summary_dict）
  emotion_detect      情绪检测快照
  food_residual       食物残留 / 进食记录
  vision_full_result  视觉综合结果（eating_window_h / has_night_meal）
  wakeword            语音事件（speech_word_per_min）
  weight_record       体重记录
  first_visit         首次使用问卷
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import PATHS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_utc_str(value) -> str:
    """把查询用的时间点统一成**写入时用的那种 UTC 字符串**。

    为什么必须有这个转换：事件写入用的是 `utc_now()`（UTC，带 Z），而调用方
    （analytics / report）习惯用 `datetime.now()` —— 那是**本地时间**。两者直接
    做字符串比较会整整错开一个时区：本机 UTC+8，刚吃完的那一餐写成 `12:39Z`，
    而查询范围是 `18:39..22:39`，于是**最近 8 小时的所有事件都查不到** ——
    表现就是"刚结束一餐，用餐次数还是 0"。

    规则：字符串原样返回（调用方自己负责）；naive datetime 视为本地时间；
    带时区的 datetime 直接换算成 UTC。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    dt = value
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.astimezone()          # naive → 按本地时区解释
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HealthStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or PATHS.db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """统一的连接生命周期：正常结束时提交，无论如何都关闭（避免长跑泄漏句柄）。"""
        con = self.connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init(self) -> None:
        with self.session() as con:
            con.executescript(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    timestamp text not null,
                    type text not null,
                    data_json text not null,
                    upload_status text not null default 'pending',
                    upload_attempts integer not null default 0,
                    last_error text
                );
                create index if not exists idx_events_upload_status
                    on events(upload_status, upload_attempts, timestamp);
                create index if not exists idx_events_type_ts
                    on events(type, timestamp);

                create table if not exists media (
                    id integer primary key autoincrement,
                    timestamp text not null,
                    type text not null,
                    path text not null,
                    metadata_json text not null
                );
                """
            )

    def add_event(self, event_type: str, data: dict[str, Any], status: str = "pending") -> int:
        self.init()
        with self.session() as con:
            cur = con.execute(
                "insert into events(timestamp, type, data_json, upload_status) values (?, ?, ?, ?)",
                (utc_now(), event_type, json.dumps(data, ensure_ascii=False), status),
            )
            return int(cur.lastrowid)

    def add_media(self, media_type: str, path: Path, metadata: dict[str, Any] | None = None) -> int:
        self.init()
        with self.session() as con:
            cur = con.execute(
                "insert into media(timestamp, type, path, metadata_json) values (?, ?, ?, ?)",
                (utc_now(), media_type, str(path), json.dumps(metadata or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def query_event_range(self, event_type: str, start, end) -> list[dict[str, Any]]:
        """按事件类型 + 时间范围查询，data_json 合并进顶层字段。

        start/end 会被 `to_utc_str()` 归一：调用方传本地 `datetime.now()` 也没关系
        （否则会与写入的 UTC 时间戳错开一个时区，见 to_utc_str 的说明）。
        """
        self.init()
        start_s = to_utc_str(start)
        end_s = to_utc_str(end)
        with self.session() as con:
            rows = con.execute(
                "select * from events where type = ? and timestamp between ? and ? order by timestamp asc",
                (event_type, start_s, end_s),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d.update(json.loads(d.pop("data_json")))
            except Exception:
                pass
            out.append(d)
        return out

    def query_events_json(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        """执行返回 data_json 列的查询并解析（供 memory / main 使用）。"""
        self.init()
        with self.session() as con:
            rows = con.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r[0]))
            except Exception:
                pass
        return out

    # ---------- 事件上传（可选云端同步） ----------
    def pending_events(self, limit: int = 20) -> list[sqlite3.Row]:
        self.init()
        with self.session() as con:
            return list(
                con.execute(
                    """
                    select * from events
                    where upload_status = 'pending' and upload_attempts < 5
                    order by timestamp asc
                    limit ?
                    """,
                    (limit,),
                )
            )

    def mark_uploaded(self, event_id: int) -> None:
        with self.session() as con:
            con.execute("update events set upload_status='uploaded', last_error=null where id=?", (event_id,))

    def mark_failed(self, event_id: int, error: str) -> None:
        with self.session() as con:
            con.execute(
                """
                update events
                set upload_attempts = upload_attempts + 1,
                    last_error = ?,
                    upload_status = case when upload_attempts + 1 >= 5 then 'failed' else 'pending' end
                where id = ?
                """,
                (error[:500], event_id),
            )
