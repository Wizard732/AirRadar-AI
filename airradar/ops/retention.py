"""retention.py — очищення старих службових даних (трасування, метрики, черга, токени). Користувацькі дані й журнал угроз НЕ чіпає."""
from __future__ import annotations

import time

from airradar.monitoring import incident_trace, metrics


def run(db, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    out = {"event_trace": incident_trace.prune(db, now), "stage_metrics": metrics.prune(db, now)}
    with db._lock:  # noqa: SLF001
        try:
            out["queue_done"] = db._conn.execute(  # noqa: SLF001
                "DELETE FROM notification_queue WHERE status IN ('sent','failed','cancelled','expired') AND created_ts < ?", (now - 14 * 86400,)).rowcount
            out["admin_tokens"] = db._conn.execute("DELETE FROM admin_tokens WHERE expires_ts < ?", (now - 86400,)).rowcount  # noqa: SLF001
        except Exception:  # noqa: BLE001 — таблиці ще немає (міграції не застосовані)
            pass
        db._conn.commit()  # noqa: SLF001
    return out
