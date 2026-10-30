"""wave_digest.py — одна «жива» зведення хвилі в каналі.

Коли в повітрі одночасно багато цілей, замість стрічки окремих постів канал отримує ОДНЕ повідомлення,
яке редагується: скільки і чого, де зараз, куди курс і орієнтовний час до великих міст. Коли цілі зникли зі
зведень на WAVE_QUIET_S — повідомлення закривається підсумком. Без води: лише факти з треків.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time

from city_coords import region_for_point
from kyiv_time import fmt as kyiv_fmt
from track_engine import build_events, build_tracks, forecast_places, haversine_km, position_at
from stats_engine import CATS, category

logger = logging.getLogger(__name__)

START_TRACKS = 3          # відкрити зведення, коли активних цілей не менше
ACTIVE_S = 15 * 60        # ціль «активна», якщо остання відмітка не старіша
WAVE_QUIET_S = 20 * 60    # скільки без активних цілей до закриття
MIN_ETA_KM = 15.0         # ближче — позиція неточна, час не вказуємо
MIN_ETA_MIN = 3
MIN_EDIT_S = 180          # не частіше за 1 редагування на 3 хв
_REGION_SHORT = None


def _short_region(slug: str) -> str:
    from alert_renderer import _OBLAST_SHORT
    from regions import region_name
    return _OBLAST_SHORT.get(slug, region_name(slug))


def compose(tracks, events, now: int) -> tuple[str, int, int]:
    """(текст, активних цілей, кількість) — None-текст, якщо активних немає."""
    act = [t for t in tracks if t.status != "stale" and now - t.last.ts <= ACTIVE_S and category(t.weapon)]
    if not act:
        return "", 0, 0
    counts: dict[str, int] = {}
    regions: dict[str, int] = {}
    etas: dict[str, int] = {}
    for t in act:
        c = category(t.weapon)
        counts[c] = counts.get(c, 0) + t.count
        lat, lon, _ = position_at(t, now)
        reg = region_for_point(lat, lon)
        if reg:
            regions[reg] = regions.get(reg, 0) + t.count
        # Ціль, названа «над/курсом на X», вже біля X: це не «через 1 хв», а невизначеність позиції.
        # Показуємо час лише для міст, до яких справді є відстань (≥ MIN_ETA_KM) і ETA ≥ MIN_ETA_MIN.
        for f in forecast_places(t, now, min_population=50000):
            if f.km >= MIN_ETA_KM and f.eta_min >= MIN_ETA_MIN:
                etas[f.name] = min(etas.get(f.name, 10**6), f.eta_min)
        if t.dest and t.speed_kmh:
            d = haversine_km((lat, lon), (t.dest[0], t.dest[1]))
            eta = round(d / t.speed_kmh * 60)
            if d >= MIN_ETA_KM and eta >= MIN_ETA_MIN:
                etas[t.dest[2]] = min(etas.get(t.dest[2], 10**6), eta)
    for e in events:
        if now - e["ts"] <= 3600:
            c = category(e["weapon"])
            if c in ("kab", "air", "fpv", "arty"):
                counts[c] = counts.get(c, 0) + e["count"]
    cnt = " · ".join(f"{CATS[k][1]} {CATS[k][0]} {counts[k]}" for k in CATS if counts.get(k))
    where = " · ".join(f"{_short_region(r)} {n}" for r, n in sorted(regions.items(), key=lambda x: -x[1])[:5])
    toward = " · ".join(f"{n} ~{m}–{round(m * 1.4)} хв" for n, m in sorted(etas.items(), key=lambda x: x[1])[:5])
    lines = [f"🌊 ХВИЛЯ · {kyiv_fmt(now)}", cnt]
    if where:
        lines.append(f"📍 Де зараз: {where}")
    if toward:
        lines.append(f"➡️ Можливо до: {toward}")
    lines += ["", "Орієнтовно: позиції зі зведень каналів. Оновлюється в цьому повідомленні.", "📡 Джерело: AirRadar AI"]
    return "\n".join(lines), len(act), sum(counts.values())


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS wave_digest (id INTEGER PRIMARY KEY CHECK (id=1), chat_id TEXT, message_id INTEGER, "
            "started_ts INTEGER, last_edit_ts INTEGER, last_active_ts INTEGER, last_hash TEXT, peak INTEGER DEFAULT 0, open INTEGER DEFAULT 0)")
        db._conn.commit()  # noqa: SLF001


def _state(db) -> dict | None:
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT chat_id, message_id, started_ts, last_edit_ts, last_active_ts, last_hash, peak, open FROM wave_digest WHERE id=1").fetchone()  # noqa: SLF001
    if not r or not r[7]:
        return None
    return dict(zip(("chat_id", "message_id", "started_ts", "last_edit_ts", "last_active_ts", "last_hash", "peak"), r[:7]))


def _save(db, **kw) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT OR IGNORE INTO wave_digest(id) VALUES (1)")  # noqa: SLF001
        sets = ", ".join(f"{k}=?" for k in kw)
        db._conn.execute(f"UPDATE wave_digest SET {sets} WHERE id=1", tuple(kw.values()))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


async def tick(db, publisher, now: int | None = None) -> str:
    """Один шаг. Повертає: 'started' | 'edited' | 'closed' | 'idle'."""
    now = int(now if now is not None else time.time())
    _ensure(db)
    rows = db.recent_observations(now - 3 * 3600)
    tracks = build_tracks(rows, now, window_s=3 * 3600)
    events = build_events(rows, now)
    text, n_tracks, _ = compose(tracks, events, now)
    st = _state(db)
    if st is None:
        if n_tracks < START_TRACKS:
            return "idle"
        sent = await publisher.send(text)
        if sent and isinstance(sent, dict):
            _save(db, chat_id=str(sent.get("chat", {}).get("id", "")), message_id=int(sent.get("message_id", 0)),
                  started_ts=now, last_edit_ts=now, last_active_ts=now, last_hash=hashlib.md5(text.encode()).hexdigest(),
                  peak=n_tracks, open=1)
            return "started"
        return "idle"
    if n_tracks:
        _save(db, last_active_ts=now, peak=max(st["peak"], n_tracks))
        h = hashlib.md5(text.encode()).hexdigest()
        if h != st["last_hash"] and now - st["last_edit_ts"] >= MIN_EDIT_S:
            if await publisher.edit(st["chat_id"], st["message_id"], text):
                _save(db, last_edit_ts=now, last_hash=h)
                return "edited"
        return "idle"
    if now - st["last_active_ts"] >= WAVE_QUIET_S:
        dur = max(1, round((st["last_active_ts"] - st["started_ts"]) / 60))
        final = (f"🌊 ХВИЛЯ ЗАВЕРШЕНА · {kyiv_fmt(st['last_active_ts'])}\n"
                 f"Тривала ~{dur} хв, пік: {st['peak']} цілей одночасно.\n\n"
                 "Нових відміток у зведеннях немає. Це не офіційний відбій — орієнтуйтесь на сирену.\n📡 Джерело: AirRadar AI")
        await publisher.edit(st["chat_id"], st["message_id"], final)
        _save(db, open=0)
        return "closed"
    return "idle"


async def digest_loop(db, publisher, interval: int = 120) -> None:
    if os.getenv("WAVE_DIGEST", "1") == "0":
        logger.info("Зведення хвилі вимкнено (WAVE_DIGEST=0)")
        return
    while True:
        try:
            res = await tick(db, publisher)
            if res != "idle":
                logger.info("wave_digest: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("wave_digest tick")
        await asyncio.sleep(interval)
