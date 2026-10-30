"""district_warn.py — предупреждения подписчикам района «возможно, летит к вам».

По трекам с ИЗМЕРЕННЫМ курсом и скоростью берём крупные города на пути (≤25 мин),
определяем их область и один раз на (цель, область) пишем подписчикам области.
Антиспам: не чаще раза в 15 минут на область; цели без измеренной скорости,
без курса и «давно не видели» не прогнозируем — лучше промолчать, чем напугать зря.
"""

from __future__ import annotations

import asyncio
import logging
import time

from city_coords import region_for_point
from regions import region_name
from track_engine import Track, _eta_range, build_tracks, compass_ua, forecast_places

logger = logging.getLogger(__name__)

HORIZON_MIN = 25
MIN_TOWN_POPULATION = 15000
REGION_COOLDOWN_S = 900
OBS_WINDOW_S = 3 * 3600
# Группа персонального фильтра типов («🎯 Типи тривог»), как в main.WEAPON_PREF_GROUPS.
GROUP = {"cruise_missile": "ballistic", "ballistic": "ballistic", "shahed": "uav", "uav": "uav"}


def _what(t: Track) -> str:
    if t.profile == "reactive":
        return "Реактивний БпЛА"
    if t.weapon == "cruise_missile":
        return "Крилата ракета"
    if t.weapon == "ballistic":
        return "Балістика"
    return "Шахед" if t.weapon == "shahed" else "БпЛА"


def warning_text(t: Track, place, region: str) -> str:
    n = f"{t.count}× " if t.count > 1 else ""
    lo, hi = _eta_range(place.eta_min, t.speed_source)
    eta = f"{lo}–{hi}" if lo != hi else str(place.eta_min)
    typical = "" if t.speed_source == "measured" else ", типова"
    icon = "🟠" if t.weapon in ("cruise_missile", "ballistic") else "🟡"
    return (f"{icon} Можливо до {place.name} ({region_name(region)}): {n}{_what(t)}, "
            f"курс {compass_ua(t.heading)}, ~{t.speed_kmh:g} км/год{typical}, орієнтовно через {eta} хв.\n"
            "Це прогноз за зведеннями каналів, не підтверджений підліт; такі прогнози збуваються рідко — орієнтуйтесь на офіційну тривогу.")


def plan(tracks: list[Track], now: int, db) -> list[tuple[str, str, str]]:
    """[(track_key, region, text)] — что слать сейчас (по одному городу, ближайшему по ETA, на область)."""
    out: list[tuple[str, str, str]] = []
    claimed: set[str] = set()
    for t in tracks:
        # Крилата ракета — самая опасная и быстрая: предупреждаем и по типовой скорости.
        # Остальные цели — только с измеренной скоростью (лучше промолчать, чем напугать зря).
        if t.status != "active" or t.heading is None or (t.speed_source != "measured" and t.weapon not in ("cruise_missile", "ballistic")):
            continue
        for place in forecast_places(t, now, horizon_min=HORIZON_MIN, min_population=MIN_TOWN_POPULATION):
            region = region_for_point(place.lat, place.lon)
            if not region or region in claimed or db.district_warned(t.key, region):
                continue
            if now - db.district_last_for_region(region) < REGION_COOLDOWN_S:
                continue
            claimed.add(region)
            out.append((t.key, region, warning_text(t, place, region)))
    return out


async def evaluate_once(db, send_to_user, now: int | None = None) -> int:
    now = now if now is not None else int(time.time())
    tracks = build_tracks(db.recent_observations(now - OBS_WINDOW_S), now, window_s=OBS_WINDOW_S)
    sent = 0
    weapon_by_key = {t.key: t.weapon for t in tracks}
    for key, region, text in plan(tracks, now, db):
        weapon = weapon_by_key.get(key, "uav")
        db.mark_district_warned(key, region, now)  # помечаем до отправки: сбой не должен вести к повторам
        for uid in db.get_subscribers(region):
            allowed = db.get_weapon_classes(uid)
            if allowed and GROUP.get(weapon, "uav") not in allowed.split(","):
                continue
            try:
                await send_to_user(uid, text)
                sent += 1
            except Exception:  # noqa: BLE001
                logger.debug("district_warn: не доставлено %s", uid, exc_info=True)
    return sent


async def district_loop(db, bot_client, interval: int = 30) -> None:
    async def send(uid: int, text: str) -> None:
        await bot_client.send_message(uid, text, link_preview=False, silent=False)
        import push_fcm
        await push_fcm.push_user(db, uid, "AirRadar", text, "warn")

    while True:
        try:
            await evaluate_once(db, send)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("district_loop: сбой прохода")
        await asyncio.sleep(interval)
