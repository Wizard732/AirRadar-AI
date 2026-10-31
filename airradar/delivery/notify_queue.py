"""notify_queue.py — персистентна черга сповіщень: ретраї, backoff, ідемпотентність, відновлення.

Статуси: pending → sending → sent | retrying → … | failed | cancelled | expired.
Унікальність (event_key, user_id, channel) гарантує, що одна подія не піде двічі в один канал.
Модуль не відправляє нічого сам: він лише керує станом; відправку виконує викликач (sender).
"""
from __future__ import annotations

import time

STATUSES = ("pending", "sending", "sent", "failed", "retrying", "cancelled", "expired")
MAX_ATTEMPTS = 5
BASE_BACKOFF_S = 5
MAX_BACKOFF_S = 600
DEFAULT_TTL_S = 15 * 60

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'threat',
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        next_attempt_ts INTEGER NOT NULL,
        created_ts INTEGER NOT NULL,
        sent_ts INTEGER,
        external_id TEXT,
        payload TEXT,
        expires_ts INTEGER NOT NULL,
        UNIQUE (event_key, user_id, channel)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nq_due ON notification_queue(status, next_attempt_ts)",
    "CREATE INDEX IF NOT EXISTS idx_nq_event ON notification_queue(event_key)",
)


def ensure_schema(db) -> None:
    done = db.__dict__.setdefault("_ar_schema_done", set())  # DDL раз на екземпляр БД, а не на кожен виклик
    if "notify_queue" in done:
        return
    with db._lock:  # noqa: SLF001
        for sql in _SCHEMA:
            db._conn.execute(sql)  # noqa: SLF001
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(notification_queue)")}  # noqa: SLF001
        if "payload" not in cols:   # таблиця створена до появи payload — додаємо колонку (без втрати даних)
            db._conn.execute("ALTER TABLE notification_queue ADD COLUMN payload TEXT")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    done.add("notify_queue")


def backoff_s(attempts: int) -> int:
    """Експоненційна затримка: 5, 10, 20, 40 … до MAX_BACKOFF_S."""
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** max(0, attempts - 1)))


def enqueue(db, event_key: str, user_id: int, channel: str, kind: str = "threat",
            ttl_s: int = DEFAULT_TTL_S, now: int | None = None, payload: str | None = None) -> bool:
    """Поставити в чергу. False, якщо така пара вже була (дедуп/ідемпотентність)."""
    now = (int(time.time()) if now is None else now)
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute(  # noqa: SLF001
            "INSERT OR IGNORE INTO notification_queue (event_key,user_id,channel,kind,status,next_attempt_ts,created_ts,expires_ts,payload) "
            "VALUES (?,?,?,?,'pending',?,?,?,?)",
            (event_key, int(user_id), channel, kind, now, now, now + ttl_s, payload),
        )
        db._conn.commit()  # noqa: SLF001
        return cur.rowcount == 1


def claim_due(db, limit: int = 50, now: int | None = None, channel: str | None = None) -> list[dict]:
    """Забрати готові до відправки записи (pending/retrying) і позначити sending. Прострочені → expired."""
    now = (int(time.time()) if now is None else now)
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "UPDATE notification_queue SET status='expired' WHERE status IN ('pending','retrying') AND expires_ts < ?", (now,))
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT * FROM notification_queue WHERE status IN ('pending','retrying') AND next_attempt_ts <= ? "
            "AND (? IS NULL OR channel = ?) ORDER BY next_attempt_ts LIMIT ?", (now, channel, channel, limit)).fetchall()
        for r in rows:
            db._conn.execute("UPDATE notification_queue SET status='sending', attempts=attempts+1 WHERE id=?", (r["id"],))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return [dict(r) for r in rows]


def mark_sent(db, qid: int, external_id: str = "", now: int | None = None) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("UPDATE notification_queue SET status='sent', sent_ts=?, external_id=?, last_error=NULL WHERE id=?",  # noqa: SLF001
                         ((int(time.time()) if now is None else now), external_id or None, qid))
        db._conn.commit()  # noqa: SLF001


def mark_failed(db, qid: int, error: str, retry_after_s: int | None = None, now: int | None = None) -> str:
    """Повернути новий статус: retrying (з backoff; retry_after_s — з rate-limit) або failed."""
    now = (int(time.time()) if now is None else now)
    with db._lock:  # noqa: SLF001
        row = db._conn.execute("SELECT attempts, expires_ts FROM notification_queue WHERE id=?", (qid,)).fetchone()  # noqa: SLF001
        if row is None:
            return "missing"
        delay = retry_after_s if retry_after_s else backoff_s(row["attempts"])
        if row["attempts"] >= MAX_ATTEMPTS or now + delay > row["expires_ts"]:
            status = "failed"
        else:
            status = "retrying"
        db._conn.execute("UPDATE notification_queue SET status=?, last_error=?, next_attempt_ts=? WHERE id=?",  # noqa: SLF001
                         (status, (error or "")[:200], now + delay, qid))
        db._conn.commit()  # noqa: SLF001
    return status


def cancel_for_event(db, event_key: str) -> int:
    """Подія завершилась до доставки — скасувати все, що ще не відправлено."""
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute(  # noqa: SLF001
            "UPDATE notification_queue SET status='cancelled' WHERE event_key=? AND status IN ('pending','retrying')", (event_key,))
        db._conn.commit()  # noqa: SLF001
        return cur.rowcount


def recover_after_restart(db) -> int:
    """Записи, що застрягли в 'sending' після падіння процесу, повертаються в retrying."""
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("UPDATE notification_queue SET status='retrying' WHERE status='sending'")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        return cur.rowcount


def stats(db, since_ts: int) -> dict:
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT status, COUNT(*) AS n FROM notification_queue WHERE created_ts >= ? GROUP BY status", (since_ts,)).fetchall()
        lat = db._conn.execute(  # noqa: SLF001
            "SELECT sent_ts - created_ts AS d FROM notification_queue WHERE status='sent' AND created_ts >= ? ORDER BY d", (since_ts,)).fetchall()
        retries = db._conn.execute(  # noqa: SLF001
            "SELECT COALESCE(SUM(MAX(attempts-1,0)),0) AS r FROM notification_queue WHERE created_ts >= ?", (since_ts,)).fetchone()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    delays = [x["d"] for x in lat if x["d"] is not None]
    out["retries"] = retries["r"]
    out["delivery_delay_p50_s"] = delays[len(delays) // 2] if delays else None  # None = unknown, не 0
    return out


def claim_one(db, event_key: str, user_id: int, channel: str) -> dict | None:
    """Забрати конкретний запис для негайної відправки (pending/retrying → sending). None — вже відправлено/в роботі."""
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT * FROM notification_queue WHERE event_key=? AND user_id=? AND channel=? AND status IN ('pending','retrying')",
            (event_key, int(user_id), channel)).fetchone()
        if row is None:
            return None
        db._conn.execute("UPDATE notification_queue SET status='sending', attempts=attempts+1 WHERE id=?", (row["id"],))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        return dict(row)


def flood_wait_seconds(exc: Exception) -> int | None:
    """Telegram FloodWait / HTTP 429 Retry-After → секунди очікування (rate limit); інакше None."""
    for attr in ("seconds", "retry_after"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and v > 0:
            return v
    return None
