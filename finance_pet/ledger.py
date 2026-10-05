"""本地轻量账本（SQLite）：记录每次对话的 token 与折算金额。

放在 ``$CODEX_HOME/finance-pet/finance.db``，单文件、零依赖。
通过 ``event_key`` 做幂等去重，因此重复扫描 rollout 不会重复计费。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key          TEXT UNIQUE,
    ts                 TEXT NOT NULL,
    thread_id          TEXT,
    turn_id            TEXT,
    session_id         TEXT,
    response_id        TEXT,
    model              TEXT,
    provider           TEXT,
    input_tokens       INTEGER DEFAULT 0,
    cached_input_tokens INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    total_tokens       INTEGER DEFAULT 0,
    cache_hit_tokens   INTEGER DEFAULT 0,
    cache_miss_tokens  INTEGER DEFAULT 0,
    completion_tokens  INTEGER DEFAULT 0,
    cost_usd           REAL DEFAULT 0,
    cost_amount        REAL DEFAULT 0,
    currency           TEXT,
    tier               TEXT,
    assumed_model      INTEGER DEFAULT 0,
    ingested_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_thread ON usage_events(thread_id);

CREATE TABLE IF NOT EXISTS scan_state (
    path    TEXT PRIMARY KEY,
    offset  INTEGER DEFAULT 0,
    mtime   REAL DEFAULT 0,
    size    INTEGER DEFAULT 0,
    model   TEXT,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class UsageEvent:
    ts: datetime
    model: str | None = None
    provider: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    session_id: str | None = None
    response_id: str | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_amount: float = 0.0
    currency: str = "CNY"
    tier: str = "peak"
    assumed_model: bool = False

    @property
    def event_key(self) -> str:
        parts = [self.session_id or "", self.turn_id or "", self.response_id or ""]
        if not any(parts):
            return f"{self.ts.isoformat()}:{self.model}"
        return ":".join(parts)


class Ledger:
    """账本读写。

    后台扫描线程与 UI 线程会并发访问同一个 SQLite 连接，而 sqlite3 的连接
    本身不是并发安全的（同时使用会抛 "bad parameter or other API misuse"），
    因此这里用一把可重入锁把所有访问串行化。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ 写
    def record(self, event: UsageEvent) -> bool:
        """写入一条用量；返回 True 表示是新记录。"""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO usage_events (
                    event_key, ts, thread_id, turn_id, session_id, response_id,
                    model, provider, input_tokens, cached_input_tokens,
                    output_tokens, total_tokens, cache_hit_tokens,
                    cache_miss_tokens, completion_tokens, cost_usd, cost_amount,
                    currency, tier, assumed_model, ingested_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_key,
                    event.ts.astimezone(timezone.utc).isoformat(),
                    event.thread_id,
                    event.turn_id,
                    event.session_id,
                    event.response_id,
                    event.model,
                    event.provider,
                    event.input_tokens,
                    event.cached_input_tokens,
                    event.output_tokens,
                    event.total_tokens,
                    event.cached_input_tokens,
                    max(0, event.input_tokens - event.cached_input_tokens),
                    event.output_tokens,
                    event.cost_usd,
                    event.cost_amount,
                    event.currency,
                    event.tier,
                    1 if event.assumed_model else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- 扫描状态
    def get_scan_state(self, path: Path) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM scan_state WHERE path = ?", (str(path),))
            return cur.fetchone()

    def set_scan_state(
        self,
        path: Path,
        *,
        offset: int,
        mtime: float,
        size: int,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scan_state (path, offset, mtime, size, model, provider)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    offset = excluded.offset,
                    mtime  = excluded.mtime,
                    size   = excluded.size,
                    model  = COALESCE(excluded.model, scan_state.model),
                    provider = COALESCE(excluded.provider, scan_state.provider)
                """,
                (str(path), offset, mtime, size, model, provider),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- 查询
    def summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        """统计 [start, end) 区间的累计消耗。"""
        with self._lock:
            row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(cost_amount), 0) AS cost,
                COALESCE(SUM(cost_usd), 0)    AS cost_usd,
                COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                COUNT(*) AS turns
            FROM usage_events
            WHERE ts >= ? AND ts < ?
            """,
            (
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
            ).fetchone()
        return dict(row) if row else {}

    def models_in(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
            """
            SELECT model, COUNT(*) AS turns,
                   COALESCE(SUM(cost_amount), 0) AS cost,
                   COALESCE(SUM(total_tokens), 0) AS tokens
            FROM usage_events
            WHERE ts >= ? AND ts < ?
            GROUP BY model
            ORDER BY cost DESC
            """,
            (
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily(self, days: int = 14, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """最近 N 天的每日消耗，用于画迷你柱状图。"""
        now = now or datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            day = (now - timedelta(days=i)).astimezone().date()
            start = datetime.combine(day, datetime.min.time()).astimezone()
            end = start + timedelta(days=1)
            row = self.summary(start, end)
            out.append({"date": day.isoformat(), "cost": row.get("cost", 0.0)})
        return out

    def last_event_time(self) -> datetime | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) AS ts FROM usage_events").fetchone()
        if not row or not row["ts"]:
            return None
        return datetime.fromisoformat(row["ts"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------- 时间段工具

def local_tz() -> timezone | Any:
    """本机当前时区（Asia/Shanghai 等）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def local_day_bounds(
    day: date | None = None,
    tz: Any | None = None,
) -> tuple[datetime, datetime]:
    """返回本地时区下某一天的 [00:00, 次日00:00)，均为 aware datetime。"""
    tz = tz or local_tz()
    day = day or datetime.now(tz).date()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def period_bounds(period: str, *, tz: timezone | None = None) -> tuple[datetime, datetime]:
    """把 ``today`` / ``7d`` / ``30d`` / ``all`` 解析成时间区间。"""
    tz = tz or local_tz()
    now = datetime.now(tz)
    if period in {"today", "day"}:
        return local_day_bounds(now.date(), tz)
    if period in {"week", "7d"}:
        return now - timedelta(days=7), now
    if period in {"month", "30d"}:
        return now - timedelta(days=30), now
    return datetime(1970, 1, 2, tzinfo=timezone.utc), now


def sum_events(events: Iterable[UsageEvent]) -> float:
    return sum(e.cost_amount for e in events)
