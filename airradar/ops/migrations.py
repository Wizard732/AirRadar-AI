"""migrations.py — версіоновані, безпечні та ідемпотентні міграції SQLite.

Правила: лише додаткові зміни (CREATE IF NOT EXISTS, індекси) — жодних DROP/DELETE даних.
Перед застосуванням на реальній БД робиться backup (backup_before), а `check_on_copy` проганяє
міграції на копії БД і перевіряє integrity_check + foreign_key_check — це викликає CI/деплой.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path


def _m1_baseline(db) -> None:
    """Базова схема створюється Database._init_schema; тут лише фіксуємо версію."""


def _m2_admin_audit(db) -> None:
    from airradar.security import admin_access
    admin_access.ensure_schema(db)


def _m3_notification_queue(db) -> None:
    from airradar.delivery import notify_queue
    notify_queue.ensure_schema(db)


def _m4_event_trace(db) -> None:
    from airradar.monitoring import incident_trace
    incident_trace.ensure_schema(db)


def _m5_metrics_alerts(db) -> None:
    from airradar.monitoring import alerts, metrics
    metrics.ensure_schema(db)
    alerts.ensure_schema(db)


def _m6_indexes(db) -> None:
    with db._lock:  # noqa: SLF001
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_nd_status ON notification_delivery(status, updated_ts)",
            "CREATE INDEX IF NOT EXISTS idx_channel_health_seen ON channel_health(last_seen)",
        ):
            try:
                db._conn.execute(sql)  # noqa: SLF001
            except sqlite3.OperationalError:
                pass  # таблиці може не бути в урізаній БД — міграція не повинна падати
        db._conn.commit()  # noqa: SLF001


MIGRATIONS = [
    (1, "baseline", _m1_baseline),
    (2, "admin_audit", _m2_admin_audit),
    (3, "notification_queue", _m3_notification_queue),
    (4, "event_trace", _m4_event_trace),
    (5, "metrics_alerts", _m5_metrics_alerts),
    (6, "extra_indexes", _m6_indexes),
]


def _ensure_table(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_ts INTEGER NOT NULL)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def current_version(db) -> int:
    _ensure_table(db)
    with db._lock:  # noqa: SLF001
        row = db._conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM schema_migrations").fetchone()  # noqa: SLF001
    return int(row["v"])


def pending(db) -> list[tuple[int, str]]:
    cur = current_version(db)
    return [(v, n) for v, n, _ in MIGRATIONS if v > cur]


def apply_pending(db, backup_before: Path | None = None) -> list[int]:
    """Застосувати відсутні міграції. Якщо є що застосовувати і задано backup_before — спершу backup."""
    todo = [m for m in MIGRATIONS if m[0] > current_version(db)]
    if not todo:
        return []
    if backup_before is not None:
        from airradar.ops import backup
        backup.sqlite_online_copy(Path(db._path), backup_before)  # noqa: SLF001
    applied = []
    for version, name, fn in todo:
        fn(db)
        with db._lock:  # noqa: SLF001
            db._conn.execute("INSERT INTO schema_migrations (version,name,applied_ts) VALUES (?,?,?)", (version, name, int(time.time())))  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
        applied.append(version)
    return applied


def check_on_copy(db_path: str | Path) -> dict:
    """Прогнати міграції на ТИМЧАСОВІЙ копії БД; повернути підсумок перевірки цілісності."""
    from airradar.ops import backup
    from database import Database
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "copy.db"
        backup.sqlite_online_copy(Path(db_path), copy)
        d = Database(str(copy))
        try:
            applied = apply_pending(d)
            with d._lock:  # noqa: SLF001
                integrity = d._conn.execute("PRAGMA integrity_check").fetchone()[0]  # noqa: SLF001
                fk = d._conn.execute("PRAGMA foreign_key_check").fetchall()  # noqa: SLF001
            return {"applied": applied, "integrity": integrity, "fk_violations": len(fk),
                    "ok": integrity == "ok" and not fk and current_version(d) == MIGRATIONS[-1][0]}
        finally:
            d.close()
