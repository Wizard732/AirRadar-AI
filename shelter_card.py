"""shelter_card.py — «Режим укриття»: одна жива картка на час тривоги.

Для користувачів із увімкненим режимом «Укриття», підписаних на область з відкритою тривогою і з хоча б однією точкою
(місце або геопозиція сторожа), бот надсилає ОДНЕ повідомлення й редагує його: скільки триває тривога і що зі зведень
летить до кожної з їхніх точок. На відбій картка закривається підсумком. Це не заміна сирени і не відбій.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from telethon import Button

import guard_places
from regions import region_name
from track_engine import build_tracks, risk_for_point, risk_text

logger = logging.getLogger(__name__)

MIN_EDIT_S = 40
OBS_WINDOW_S = 3 * 3600


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS shelter_cards (user_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, "
            "started_ts INTEGER NOT NULL, last_edit_ts INTEGER NOT NULL, last_hash TEXT NOT NULL DEFAULT '', regions TEXT NOT NULL DEFAULT '')")
        db._conn.commit()  # noqa: SLF001


def open_regions(db, now: int, stale_s: int = 3 * 3600) -> dict[str, int]:
    """{slug: since_ts} відкритих тривог (таблицю веде alarm_notices)."""
    try:
        with db._lock:  # noqa: SLF001
            rows = db._conn.execute("SELECT region, since_ts FROM alarm_state WHERE open=1 AND last_ts>=?", (now - stale_s,)).fetchall()  # noqa: SLF001
        return {r: s for r, s in rows}
    except Exception:  # noqa: BLE001 — таблиці ще немає
        return {}


def shelter_users(db) -> list[int]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT user_id FROM user_prefs WHERE shelter_mode=1").fetchall()  # noqa: SLF001
    return [r[0] for r in rows]


def user_targets(db, uid: int, now: int) -> list[tuple[str, tuple[float, float], float]]:
    out = [(p["name"], (p["lat"], p["lon"]), float(p["radius_km"] or 10)) for p in guard_places.list_places(db, uid)]
    g = db.get_guard(uid)
    if g and g["enabled"] and g["lat"] is not None and (not g["live"] or now - int(g["updated_ts"] or 0) <= 600):
        out.append(("Моя точка", (g["lat"], g["lon"]), float(g["radius_km"] or 10)))
    return out


def compose(db, uid: int, regions: list[str], since: int, tracks, now: int) -> str:
    mins = max(1, round((now - since) / 60))
    lines = [f"🚨 ТРИВОГА — {', '.join(region_name(r) for r in regions)} · {mins} хв", ""]
    for name, point, radius in user_targets(db, uid, now):
        risks = risk_for_point(tracks, point, now, imminent_km=radius, watch_km=radius * 3)
        lines.append(f"📍 {name}: " + (risk_text(risks[0]) if risks else "поруч цілей зі зведень не бачу"))
    lines += ["", "Це не відбій. Орієнтуйтесь на сирену; у разі небезпеки — в укриття. Картка оновлюється сама."]
    return "\n".join(lines)


def _buttons(map_url: str):
    rows = [[Button.inline("🏚 Найближче укриття", data="shl")]]
    if map_url:
        from menu_screens import webapp_button
        rows.append([webapp_button("🗺 Карта", map_url.rstrip("/") + "/")])
    return rows


async def tick(db, send, edit, map_url: str = "", now: int | None = None) -> dict[str, int]:
    """send(uid, text, buttons) -> message_id; edit(uid, message_id, text, buttons|None)."""
    now = int(now if now is not None else time.time())
    _ensure(db)
    opened = open_regions(db, now)
    stats = {"sent": 0, "edited": 0, "closed": 0}
    with db._lock:  # noqa: SLF001
        cards = {r[0]: r for r in db._conn.execute("SELECT user_id, message_id, started_ts, last_edit_ts, last_hash, regions FROM shelter_cards").fetchall()}  # noqa: SLF001
    tracks = None
    for uid in shelter_users(db):
        subs = set(db.user_subscriptions(uid))
        home = db.get_home_region(uid)
        if home:
            subs.add(home)
        hit = sorted(r for r in opened if r in subs)
        has_targets = bool(user_targets(db, uid, now))
        card = cards.get(uid)
        if hit and has_targets:
            if tracks is None:
                tracks = build_tracks(db.recent_observations(now - OBS_WINDOW_S), now, window_s=OBS_WINDOW_S)
            live = [t for t in tracks if t.status != "stale"]
            since = min(opened[r] for r in hit)
            text = compose(db, uid, hit, since, live, now)
            h = hashlib.md5(text.encode()).hexdigest()
            if card is None:
                try:
                    mid = await send(uid, text, _buttons(map_url))
                except Exception:  # noqa: BLE001
                    logger.warning("shelter_card: не надіслано %s", uid, exc_info=True)
                    continue
                with db._lock:  # noqa: SLF001
                    db._conn.execute("INSERT OR REPLACE INTO shelter_cards VALUES (?,?,?,?,?,?)", (uid, int(mid), since, now, h, ",".join(hit)))  # noqa: SLF001
                    db._conn.commit()  # noqa: SLF001
                stats["sent"] += 1
            elif h != card[4] and now - card[3] >= MIN_EDIT_S:
                try:
                    await edit(uid, card[1], text, _buttons(map_url))
                except Exception:  # noqa: BLE001
                    logger.debug("shelter_card: не відредаговано %s", uid, exc_info=True)
                with db._lock:  # noqa: SLF001
                    db._conn.execute("UPDATE shelter_cards SET last_edit_ts=?, last_hash=? WHERE user_id=?", (now, h, uid))  # noqa: SLF001
                    db._conn.commit()  # noqa: SLF001
                stats["edited"] += 1
        elif card is not None:
            names = ", ".join(region_name(r) for r in card[5].split(",") if r)
            mins = max(1, round((now - card[2]) / 60))
            try:
                await edit(uid, card[1], f"🟢 Тривога завершена — {names} · тривала ~{mins} хв.\nЦе за зведеннями каналів; орієнтуйтесь на офіційний відбій.", None)
            except Exception:  # noqa: BLE001
                logger.debug("shelter_card: закриття не вдалось %s", uid, exc_info=True)
            with db._lock:  # noqa: SLF001
                db._conn.execute("DELETE FROM shelter_cards WHERE user_id=?", (uid,))  # noqa: SLF001
                db._conn.commit()  # noqa: SLF001
            stats["closed"] += 1
    return stats


async def card_loop(db, bot_client, map_url: str = "", interval: int = 30) -> None:
    async def send(uid, text, buttons):
        msg = await bot_client.send_message(uid, text, buttons=buttons, silent=False, link_preview=False)
        return msg.id

    async def edit(uid, mid, text, buttons):
        await bot_client.edit_message(uid, mid, text, buttons=buttons, link_preview=False)

    while True:
        try:
            if not bot_client.is_connected():   # на старті цикл стартує раніше за підключення
                await asyncio.sleep(5)
                continue
            await tick(db, send, edit, map_url)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("shelter_card tick")
        await asyncio.sleep(interval)
