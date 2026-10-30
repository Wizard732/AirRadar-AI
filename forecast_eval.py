"""forecast_eval.py — чесна перевірка власних прогнозів радара.

Кожен раз, коли трек отримує прогноз «можливо до: Місто ~N хв», пишемо рядок (трек, місто, час).
Через вікно перевіряємо за подальшими відмітками ТОГО Ж треку: пройшла ціль у межах HIT_KM від міста
до ETA+30 хв (влучання), чи трек продовжився в інший бік (промах). Якщо трек просто зник зі зведень,
результат невідомий і в точність не входить — завищувати метрику не можна.
"""
from __future__ import annotations

import time
from typing import Any

from track_engine import forecast_places, haversine_km

HIT_KM = 15.0
WINDOW_AFTER_ETA_S = 30 * 60
MIN_RESOLVED = 10


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS forecast_log (id INTEGER PRIMARY KEY AUTOINCREMENT, track_key TEXT NOT NULL, "
            "town TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, made_ts INTEGER NOT NULL, eta_ts INTEGER NOT NULL, "
            "speed_source TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '', UNIQUE(track_key, town))")
        try:
            db._conn.execute("ALTER TABLE forecast_log ADD COLUMN region TEXT NOT NULL DEFAULT ''")  # noqa: SLF001
        except Exception:  # noqa: BLE001 — колонка вже є
            pass
        db._conn.commit()  # noqa: SLF001


def log_forecasts(db, tracks, now: int) -> int:
    ensure(db)
    n = 0
    from city_coords import region_for_point
    with db._lock:  # noqa: SLF001
        for t in tracks:
            for f in forecast_places(t, now):
                cur = db._conn.execute(  # noqa: SLF001
                    "INSERT OR IGNORE INTO forecast_log(track_key, town, lat, lon, made_ts, eta_ts, speed_source, region) VALUES (?,?,?,?,?,?,?,?)",
                    (t.key, f.name, f.lat, f.lon, now, now + f.eta_min * 60, t.speed_source, region_for_point(f.lat, f.lon) or ""))
                n += cur.rowcount
        db._conn.commit()  # noqa: SLF001
    return n


def evaluate(db, tracks, now: int) -> int:
    """Закрити прогнози, що дозріли. Повертає кількість проставлених результатів."""
    ensure(db)
    by_key = {t.key: t for t in tracks}
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT id, track_key, lat, lon, made_ts, eta_ts FROM forecast_log WHERE outcome='' AND eta_ts + ? <= ?",
            (WINDOW_AFTER_ETA_S, now)).fetchall()
    done = 0
    updates = []
    for rid, key, lat, lon, made, eta in rows:
        t = by_key.get(key)
        outcome = "unknown"
        if t is not None:
            near = any(haversine_km((p.lat, p.lon), (lat, lon)) <= HIT_KM and made - 300 <= p.ts <= eta + WINDOW_AFTER_ETA_S
                       for p in t.points)
            if near:
                outcome = "hit"
            elif t.points[-1].ts >= eta - 5 * 60 and len([p for p in t.points if p.ts > made]) >= 2:
                outcome = "miss"          # трек живий і дійшов до ETA, але повз місто
        updates.append((outcome, rid)); done += 1
    if updates:
        with db._lock:  # noqa: SLF001
            db._conn.executemany("UPDATE forecast_log SET outcome=? WHERE id=?", updates)  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
    return done


def accuracy(db, now: int | None = None, days: int = 7) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT outcome, COUNT(*) FROM forecast_log WHERE made_ts>=? AND outcome!='' GROUP BY 1", (now - days * 86400,)).fetchall()
    c = {k: n for k, n in rows}
    hits, misses = c.get("hit", 0), c.get("miss", 0)
    resolved = hits + misses
    return {"days": days, "hits": hits, "misses": misses, "unknown": c.get("unknown", 0), "resolved": resolved,
            "pct": round(100 * hits / resolved) if resolved >= MIN_RESOLVED else None}


def accuracy_regions(db, now: int | None = None, days: int = 30) -> dict[str, Any]:
    """Точність прогнозів за N днів: загалом і по областях міста-цілі. Мало даних — pct=None, чесно «замало»."""
    now = int(now if now is not None else time.time())
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT region, outcome, COUNT(*) FROM forecast_log WHERE made_ts>=? AND outcome IN ('hit','miss') GROUP BY 1, 2",
            (now - days * 86400,)).fetchall()
    by: dict[str, dict[str, int]] = {}
    for region, outcome, n in rows:
        by.setdefault(region or "", {"hit": 0, "miss": 0})[outcome] = n
    out = []
    for region, c in by.items():
        resolved = c["hit"] + c["miss"]
        out.append({"slug": region, "hits": c["hit"], "misses": c["miss"], "resolved": resolved,
                    "pct": round(100 * c["hit"] / resolved) if resolved >= 5 else None})
    out.sort(key=lambda x: -x["resolved"])
    total = accuracy(db, now, days)
    return {"days": days, "overall": total, "regions": out}
