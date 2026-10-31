"""alerts.py — адмін-алерти з порогами, cooldown, дедуплікацією та повідомленням про відновлення.

check(name, bad, message): якщо стан «поганий» — шле алерт не частіше, ніж раз на cooldown;
коли стан знову «добрий» після алерту — один раз шле «відновлено». Стан зберігається в SQLite
(переживає перезапуск), щоб після рестарту не було шторму повторних повідомлень.
"""
from __future__ import annotations

import os
import shutil
import time

_SCHEMA = ("CREATE TABLE IF NOT EXISTS alert_state (name TEXT PRIMARY KEY, active INTEGER NOT NULL, since_ts INTEGER NOT NULL, last_sent_ts INTEGER NOT NULL)",)

DEFAULT_THRESHOLDS = {
    "source_silent_s": 3 * 3600,
    "queue_pending_max": 200,
    "disk_free_min_mb": 500,
    "pipeline_p95_max_ms": 30000,
    "push_fail_ratio_max": 0.5,
    "backup_max_age_s": 36 * 3600,
}
COOLDOWN_S = 3600


def thresholds() -> dict:
    """Пороги з env (ALERT_<NAME>) поверх значень за замовчуванням."""
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in DEFAULT_THRESHOLDS.items():
        raw = os.getenv("ALERT_" + k.upper())
        if raw:
            try:
                out[k] = type(v)(float(raw))
            except ValueError:
                pass
    return out


def ensure_schema(db) -> None:
    done = db.__dict__.setdefault("_ar_schema_done", set())  # DDL раз на екземпляр БД, а не на кожен виклик
    if "alerts" in done:
        return
    with db._lock:  # noqa: SLF001
        for sql in _SCHEMA:
            db._conn.execute(sql)  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    done.add("alerts")


def check(db, name: str, bad: bool, message: str, cooldown_s: int = COOLDOWN_S, now: int | None = None) -> str | None:
    """Повернути текст для відправки адмінам або None (нічого слати)."""
    now = (int(time.time()) if now is None else now)
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        row = db._conn.execute("SELECT * FROM alert_state WHERE name=?", (name,)).fetchone()  # noqa: SLF001
        if bad:
            if row is None or not row["active"]:
                db._conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?,?)", (name, 1, now, now))  # noqa: SLF001
                db._conn.commit()  # noqa: SLF001
                return f"🚨 {message}"
            if now - row["last_sent_ts"] >= cooldown_s:
                db._conn.execute("UPDATE alert_state SET last_sent_ts=? WHERE name=?", (now, name))  # noqa: SLF001
                db._conn.commit()  # noqa: SLF001
                return f"🚨 (триває) {message}"
            return None
        if row is not None and row["active"]:
            db._conn.execute("UPDATE alert_state SET active=0, last_sent_ts=? WHERE name=?", (now, name))  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
            return f"✅ Відновлено: {name}"
    return None


def disk_free_mb(path: str = ".") -> int | None:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return None


def evaluate(db, *, now: int | None = None) -> list[str]:
    """Обчислити всі алерти за поточним станом. Метрика unknown → алерт не спрацьовує (і не «відновлюється»)."""
    from airradar.delivery import notify_queue
    from airradar.monitoring import metrics
    from airradar.ops import backup
    now = (int(time.time()) if now is None else now)
    th = thresholds()
    msgs: list[str | None] = []
    free = disk_free_mb()
    if free is not None:
        msgs.append(check(db, "disk_space", free < th["disk_free_min_mb"], f"Мало місця на диску: {free} МБ", now=now))
    ns = notify_queue.stats(db, now - 3600)
    waiting = ns["pending"] + ns["retrying"]
    msgs.append(check(db, "queue_growth", waiting > th["queue_pending_max"], f"Черга сповіщень росте: {waiting}", now=now))
    sent_fail = ns["sent"] + ns["failed"]
    if sent_fail >= 10:
        msgs.append(check(db, "push_failures", ns["failed"] / sent_fail > th["push_fail_ratio_max"],
                          f"Систематичні збої доставки: {ns['failed']}/{sent_fail}", now=now))
    total = metrics.stage_summary(db, "total", 3600, now)
    if total["p95"] is not None:
        msgs.append(check(db, "pipeline_latency", total["p95"] > th["pipeline_p95_max_ms"], f"p95 обробки {total['p95']} мс", now=now))
    age = backup.last_backup_age_s(now=now)
    if age is not None:
        msgs.append(check(db, "backup_stale", age > th["backup_max_age_s"], f"Остання резервна копія {age // 3600} год тому", now=now))
    try:
        rows = db.all_channel_health()
        silent = [r["channel"] for r in rows if not r.get("disabled") and r.get("last_seen") and now - r["last_seen"] > th["source_silent_s"]]
        msgs.append(check(db, "sources_silent", bool(silent), f"Джерела мовчать: {', '.join(silent[:5])}", now=now))
    except Exception:  # noqa: BLE001
        pass
    return [m for m in msgs if m]
