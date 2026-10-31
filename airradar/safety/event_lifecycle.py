"""event_lifecycle.py — єдиний життєвий цикл події: Created → Observed → Verified → Updated → Resolved → Archived.

Усі компоненти питають стан тут, тож «один вважає подію активною, другий — завершеною» не трапляється.
Стан рухається лише вперед (Updated може повторюватись); Archived — кінцевий.
"""
from __future__ import annotations

import time
from enum import Enum


class Stage(str, Enum):
    CREATED = "created"
    OBSERVED = "observed"
    VERIFIED = "verified"
    UPDATED = "updated"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


_ORDER = {Stage.CREATED: 0, Stage.OBSERVED: 1, Stage.VERIFIED: 2, Stage.UPDATED: 3, Stage.RESOLVED: 4, Stage.ARCHIVED: 5}
ACTIVE = {Stage.CREATED, Stage.OBSERVED, Stage.VERIFIED, Stage.UPDATED}
ARCHIVE_AFTER_S = 6 * 3600


def can_advance(cur: Stage | None, new: Stage) -> bool:
    if cur is None:
        return new is Stage.CREATED
    if cur is Stage.ARCHIVED:
        return False
    if cur is Stage.UPDATED and new is Stage.UPDATED:
        return True
    if new is Stage.UPDATED and cur is Stage.RESOLVED:
        return False
    return _ORDER[new] > _ORDER[cur]


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS event_lifecycle (event_key TEXT PRIMARY KEY, stage TEXT NOT NULL, "
            "created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL)")
        db._conn.commit()  # noqa: SLF001


def stage_of(db, key: str) -> Stage | None:
    _ensure(db)
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT stage FROM event_lifecycle WHERE event_key=?", (key,)).fetchone()  # noqa: SLF001
    return Stage(r[0]) if r else None


def advance(db, key: str, new: Stage, now: int | None = None) -> bool:
    """Перевести подію в новий стан; False, якщо перехід недозволений. Нова подія завжди починається з CREATED."""
    if not key:
        return False
    now = int(now if now is not None else time.time())
    cur = stage_of(db, key)
    if cur is None and new is not Stage.CREATED:
        advance(db, key, Stage.CREATED, now)
        cur = Stage.CREATED
    if not can_advance(cur, new):
        return False
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "INSERT INTO event_lifecycle(event_key, stage, created_ts, updated_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(event_key) DO UPDATE SET stage=excluded.stage, updated_ts=excluded.updated_ts",
            (key, new.value, now, now))
        db._conn.commit()  # noqa: SLF001
    return True


def is_active(db, key: str) -> bool:
    s = stage_of(db, key)
    return s in ACTIVE if s else False


def archive_old(db, now: int | None = None) -> int:
    """Resolved старше ARCHIVE_AFTER_S → Archived; повертає кількість."""
    _ensure(db)
    now = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute(  # noqa: SLF001
            "UPDATE event_lifecycle SET stage='archived', updated_ts=? WHERE stage='resolved' AND updated_ts<?",
            (now, now - ARCHIVE_AFTER_S))
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount


def resolve_active(db, now: int | None = None) -> int:
    """Офіційний відбій: усі активні події стають Resolved (виклик — тільки з перевіреного відбою)."""
    _ensure(db)
    now = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute(  # noqa: SLF001
            "UPDATE event_lifecycle SET stage='resolved', updated_ts=? WHERE stage IN ('created','observed','verified','updated')",
            (now,))
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount
