"""Персистентний журнал буфера агрегатора (R1): алерт, що чекає вікна публікації, переживає крах/рестарт.

Запис робиться при submit(), видалення — після спроби публікації. Після старту main викликає
AlertAggregator.recover(): свіжі записи публікуються, застарілі відкидаються (свіжість важливіша за повноту).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time

from aggregator import PendingAlert
from incident_fusion import IncidentFact

logger = logging.getLogger(__name__)


def _dump(alert: PendingAlert) -> str:
    d = dataclasses.asdict(alert)
    d.pop("pid", None)
    return json.dumps(d, ensure_ascii=False)


def _load(payload: str) -> PendingAlert:
    d = json.loads(payload)
    d["fact"] = IncidentFact(**d["fact"])
    d.pop("pid", None)
    return PendingAlert(**d)


class AggStore:
    def __init__(self, db) -> None:
        self._db = db
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                "CREATE TABLE IF NOT EXISTS agg_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_ts INTEGER NOT NULL, payload TEXT NOT NULL)")
            db._conn.commit()  # noqa: SLF001

    def save(self, alert: PendingAlert) -> int:
        try:
            with self._db._lock:  # noqa: SLF001
                cur = self._db._conn.execute(  # noqa: SLF001
                    "INSERT INTO agg_pending(created_ts, payload) VALUES (?, ?)", (int(time.time()), _dump(alert)))
                self._db._conn.commit()  # noqa: SLF001
                return int(cur.lastrowid)
        except Exception:  # noqa: BLE001 — журнал не повинен блокувати публікацію
            logger.warning("agg_store: не вдалося записати алерт", exc_info=True)
            return 0

    def delete(self, ids: list[int]) -> None:
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            with self._db._lock:  # noqa: SLF001
                self._db._conn.executemany("DELETE FROM agg_pending WHERE id=?", [(i,) for i in ids])  # noqa: SLF001
                self._db._conn.commit()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.warning("agg_store: не вдалося видалити записи", exc_info=True)

    def load(self, max_age_s: int) -> list[PendingAlert]:
        """Свіжі записи (з виставленим pid); застарілі та пошкоджені видаляються."""
        out, drop = [], []
        try:
            with self._db._lock:  # noqa: SLF001
                rows = self._db._conn.execute("SELECT id, created_ts, payload FROM agg_pending ORDER BY id").fetchall()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.warning("agg_store: не вдалося прочитати журнал", exc_info=True)
            return []
        now = time.time()
        for r in rows:
            if now - r["created_ts"] > max_age_s:
                drop.append(r["id"]); continue
            try:
                a = _load(r["payload"]); a.pid = r["id"]; out.append(a)
            except Exception:  # noqa: BLE001
                logger.warning("agg_store: запис %s пошкоджений, видалено", r["id"], exc_info=True)
                drop.append(r["id"])
        self.delete(drop)
        return out
