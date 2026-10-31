"""metrics.py — тривалість етапів конвеєра (p50/p95/p99) з історією у SQLite.

Недоступна метрика = None (відображається як 'unknown'), а не 0.
"""
from __future__ import annotations

import time

STAGE_NAMES = ("ingest", "parse", "geocode", "ai", "notify_build", "delivery", "total")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS stage_metrics (ts INTEGER NOT NULL, stage TEXT NOT NULL, ms INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_stage_metrics ON stage_metrics(stage, ts)",
)
RETENTION_S = 30 * 86400
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def ensure_schema(db) -> None:
    done = db.__dict__.setdefault("_ar_schema_done", set())  # DDL раз на екземпляр БД, а не на кожен виклик
    if "metrics" in done:
        return
    with db._lock:  # noqa: SLF001
        for sql in _SCHEMA:
            db._conn.execute(sql)  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    done.add("metrics")


def record_stage(db, stage: str, ms: float, now: int | None = None) -> None:
    try:
        ensure_schema(db)
        with db._lock:  # noqa: SLF001
            db._conn.execute("INSERT INTO stage_metrics (ts,stage,ms) VALUES (?,?,?)", ((int(time.time()) if now is None else now), stage, int(ms)))  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def percentile(sorted_vals: list[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def stage_summary(db, stage: str, window_s: int, now: int | None = None) -> dict:
    ensure_schema(db)
    since = ((int(time.time()) if now is None else now)) - window_s
    with db._lock:  # noqa: SLF001
        vals = sorted(r[0] for r in db._conn.execute("SELECT ms FROM stage_metrics WHERE stage=? AND ts>=?", (stage, since)))  # noqa: SLF001
    return {"stage": stage, "count": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95),
            "p99": percentile(vals, 99), "avg": (sum(vals) // len(vals)) if vals else None}


def prune(db, now: int | None = None) -> int:
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM stage_metrics WHERE ts < ?", (((int(time.time()) if now is None else now)) - RETENTION_S,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        return cur.rowcount
