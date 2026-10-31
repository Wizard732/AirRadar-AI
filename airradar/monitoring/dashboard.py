"""dashboard.py — зведення стану системи для адмін-дашборда та команд бота.

Недоступна метрика = None (у відповіді 'unknown'), ніколи не 0. Секретів у виводі немає.
"""
from __future__ import annotations

import hmac
import os
import shutil
import sqlite3
import time
from pathlib import Path

from airradar.delivery import notify_queue
from airradar.monitoring import metrics
from airradar.ops import backup, migrations

PERIODS = metrics.WINDOWS  # 1h / 24h / 7d / 30d


def token_ok(supplied: str) -> bool:
    """Сталий за часом порівняння з ADMIN_DASH_TOKEN; якщо токен не заданий — дашборд вимкнений."""
    expected = os.getenv("ADMIN_DASH_TOKEN", "")
    return bool(expected) and len(expected) >= 16 and hmac.compare_digest(supplied or "", expected)


def _count(db, sql: str, args: tuple = ()):
    try:
        with db._lock:  # noqa: SLF001
            return db._conn.execute(sql, args).fetchone()[0]  # noqa: SLF001
    except sqlite3.Error:
        return None


def _mem_cpu() -> dict:
    out: dict = {"load1": None, "mem_rss_mb": None}
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["mem_rss_mb"] = round(rss / (1024 * 1024 if os.uname().sysname == "Darwin" else 1024), 1)
    except Exception:  # noqa: BLE001
        pass
    return out


def _legacy_latency(db, window: int) -> dict:
    """Існуюча таблиця pipeline_latency; нуль замірів → None (unknown), а не 0."""
    r = db.latency_percentiles(window)
    if not r.get("samples"):
        return {"p50_ms": None, "p95_ms": None, "samples": 0}
    return r


def collect(db, period: str = "24h", now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    window = PERIODS.get(period, PERIODS["24h"])
    since = now - window
    sources = []
    try:
        for r in db.all_channel_health():
            last = r.get("last_seen")
            sources.append({"channel": r["channel"], "last_seen_age_s": (now - last) if last else None,
                            "errors": r.get("error_count"), "last_error": (r.get("last_error") or "")[:120] or None,
                            "disabled": bool(r.get("disabled"))})
    except Exception:  # noqa: BLE001
        sources = None
    db_path = Path(getattr(db, "_path", "") or "")
    try:
        db_size = db_path.stat().st_size
    except OSError:
        db_size = None
    try:
        disk_free = shutil.disk_usage(db_path.parent if db_path.parent.exists() else ".").free
    except OSError:
        disk_free = None
    stages = {s: metrics.stage_summary(db, s, window, now) for s in metrics.STAGE_NAMES}
    return {
        "generated": now, "period": period,
        "sources": sources,
        "processing": {
            "new_events": _count(db, "SELECT COUNT(*) FROM threats WHERE ts >= ?", (since,)),
            "latency": stages,
            "pipeline_legacy": _legacy_latency(db, window),
        },
        "delivery": notify_queue.stats(db, since),
        "system": {
            "sqlite_ok": _count(db, "SELECT 1") == 1,
            "db_size_mb": None if db_size is None else round(db_size / 1e6, 1),
            "disk_free_mb": None if disk_free is None else disk_free // (1024 * 1024),
            "schema_version": migrations.current_version(db), "schema_pending": [v for v, _ in migrations.pending(db)],
            "last_backup_age_s": backup.last_backup_age_s(now=now),
            **_mem_cpu(),
        },
    }


def _fmt(v, unit: str = "") -> str:
    return "unknown" if v is None else f"{v}{unit}"


def render_text(d: dict) -> str:
    s, dl, p = d["system"], d["delivery"], d["processing"]
    lines = [f"🩺 <b>Стан системи</b> ({d['period']})", "",
             f"SQLite: {'✅' if s['sqlite_ok'] else '🔴'}, БД {_fmt(s['db_size_mb'], ' МБ')}, диск вільно {_fmt(s['disk_free_mb'], ' МБ')}",
             f"Схема: v{s['schema_version']}" + (f" (очікують {s['schema_pending']})" if s["schema_pending"] else ""),
             f"RSS {_fmt(s['mem_rss_mb'], ' МБ')}, load {_fmt(s['load1'])}",
             f"Backup: {_fmt(None if s['last_backup_age_s'] is None else s['last_backup_age_s'] // 3600, ' год тому')}",
             "", f"Події: нових {_fmt(p['new_events'])}",
             "Затримки (p50/p95/p99, мс): " + ", ".join(f"{k} {_fmt(v['p50'])}/{_fmt(v['p95'])}/{_fmt(v['p99'])}" for k, v in p["latency"].items() if v["count"]) or "unknown",
             "", f"Сповіщення: sent {dl['sent']}, failed {dl['failed']}, retrying {dl['retrying']}, pending {dl['pending']}, "
                 f"повторів {dl['retries']}, затримка p50 {_fmt(dl['delivery_delay_p50_s'], ' с')}"]
    srcs = d["sources"]
    if srcs is None:
        lines.append("Джерела: unknown")
    else:
        lines.append(f"Джерела: {len(srcs)}, із помилками {sum(1 for x in srcs if x['errors'])}")
    return "\n".join(lines)


def render_html(d: dict) -> str:
    import html, json
    body = html.escape(json.dumps(d, ensure_ascii=False, indent=2))
    links = " · ".join(f'<a href="?period={p}">{p}</a>' for p in PERIODS)
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>AirRadar health</title><meta name=robots content=noindex>"
            f"<body style='font:14px system-ui;margin:16px'><h1>AirRadar health</h1><p>{links}</p>"
            f"<pre style='white-space:pre-wrap'>{body}</pre>")
