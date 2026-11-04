"""Журнал вхідних постів (R2/R3): пост, що не пройшов конвейер через збій, не губиться, а повторюється.

Запис створюється після фільтра/дедуплікації, перед основною обробкою; видаляється після успішної
(або свідомо відкинутої) обробки. Збій лишає запис; inbox_retry_loop повторює його до MAX_ATTEMPTS
протягом MAX_AGE_S — свіжість важливіша за повноту.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_AGE_S = 600
MIN_IDLE_S = 60     # запис без помилки, старший за це, вважається покинутим крахом процесу


class Inbox:
    def __init__(self, db) -> None:
        self._db = db
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                "CREATE TABLE IF NOT EXISTS ingest_inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER NOT NULL, "
                "source TEXT NOT NULL, event_ts INTEGER NOT NULL, text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "last_error TEXT NOT NULL DEFAULT '', failed_ts INTEGER NOT NULL DEFAULT 0)")
            db._conn.commit()  # noqa: SLF001

    @classmethod
    def of(cls, db) -> "Inbox":
        inbox = getattr(db, "_ingest_inbox", None)
        if inbox is None:
            inbox = db._ingest_inbox = cls(db)
        return inbox

    def _exec(self, sql: str, args: tuple = ()):
        try:
            with self._db._lock:  # noqa: SLF001
                cur = self._db._conn.execute(sql, args)  # noqa: SLF001
                self._db._conn.commit()  # noqa: SLF001
                return cur
        except Exception:  # noqa: BLE001 — журнал не повинен ламати конвейер
            logger.warning("inbox: помилка SQL", exc_info=True)
            return None

    def add(self, source: str, event_ts: int, text: str) -> int:
        cur = self._exec("INSERT INTO ingest_inbox(created_ts, source, event_ts, text) VALUES (?,?,?,?)",
                         (int(time.time()), str(source), int(event_ts), text))
        return int(cur.lastrowid) if cur is not None else 0

    def done(self, inbox_id: int) -> None:
        if inbox_id:
            self._exec("DELETE FROM ingest_inbox WHERE id=?", (inbox_id,))

    def fail(self, inbox_id: int, error: str) -> None:
        if inbox_id:
            self._exec("UPDATE ingest_inbox SET last_error=?, failed_ts=? WHERE id=?", (error[:300], int(time.time()), inbox_id))

    def claim_retry(self, now: int | None = None) -> list[dict]:
        """Записи до повтору (attempts++ одразу). Прострочені/вичерпані видаляються."""
        now = int(now if now is not None else time.time())
        self._exec("DELETE FROM ingest_inbox WHERE created_ts < ? OR attempts >= ?", (now - MAX_AGE_S, MAX_ATTEMPTS))
        try:
            with self._db._lock:  # noqa: SLF001
                rows = self._db._conn.execute(  # noqa: SLF001
                    "SELECT id, source, event_ts, text, attempts FROM ingest_inbox "
                    "WHERE (failed_ts > 0 AND failed_ts <= ?) OR (failed_ts = 0 AND created_ts <= ?) ORDER BY id",
                    (now - 5, now - MIN_IDLE_S)).fetchall()
        except Exception:  # noqa: BLE001
            logger.warning("inbox: не вдалося прочитати чергу повтору", exc_info=True)
            return []
        out = [dict(r) for r in rows]
        for r in out:
            self._exec("UPDATE ingest_inbox SET attempts=attempts+1, failed_ts=0, created_ts=created_ts WHERE id=?", (r["id"],))
        return out
