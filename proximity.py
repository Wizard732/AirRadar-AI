"""proximity.py — персональные предупреждения «цель приближается к вам».

Цикл раз в несколько секунд: наблюдения → треки → для каждого включённого
«гео-сторожа» (live-локация или фиксированная точка) считаем риск и шлём
сообщение в Telegram (обычное, не silent — на телефоне приходит push со
звуком уведомления). Антиспам: пишем при ПОВЫШЕНИИ уровня, при заметном
сближении на том же уровне и при удалении цели — один раз «віддалилась».
"""

from __future__ import annotations

import asyncio
import logging
import time

from track_engine import Risk, Track, bearing_deg, build_events, build_tracks, compass_ua, haversine_km, risk_for_point, risk_text

logger = logging.getLogger(__name__)

LEVEL_RANK = {"watch": 1, "imminent": 2, "over": 3}
RENOTIFY_MIN_S = {"watch": 600, "imminent": 150, "over": 90}
RENOTIFY_CLOSER_KM = 3.0
OBS_WINDOW_S = 3 * 3600


def decide(prev: dict | None, risk: Risk, now: int) -> bool:
    """Слать ли сообщение по этой цели (чистая функция — легко тестировать)."""
    if prev is None:
        return True
    if LEVEL_RANK[risk.level] > LEVEL_RANK.get(prev["level"], 0):
        return True
    waited = now - int(prev["sent_ts"])
    closer = float(prev["distance"]) - risk.distance_km
    return waited >= RENOTIFY_MIN_S[risk.level] and closer >= RENOTIFY_CLOSER_KM


def guard_message(risk: Risk, map_url: str = "", label: str = "") -> str:
    text = risk_text(risk)
    if label:
        text = f"📍 {label}\n{text}"
    tail = "\nОрієнтовно, за даними моніторингових каналів. Не чекайте — в укриття, якщо є тривога."
    if risk.level == "over":
        tail = "\n🚨 В укриття зараз."
    if map_url:
        tail += f"\n🗺 {map_url.rstrip('/')}/radar"
    return text + tail


def retreat_message(track: Track, label: str = "") -> str:
    return "🟢 Ціль віддалилась від " + (f"«{label}»" if label else "вашої точки") + ". Слідкуємо далі."


# Миттєві події без траєкторії (КАБ, ракети, авіація, РСЗВ): попереджаємо за радіусом типу зброї.
EVENT_NAME = {"kab": "КАБ", "missile": "Ракета", "air_missile": "Ракета", "coastal_missile": "Ракета",
              "aviation": "Авіація", "tac_aviation": "Тактична авіація", "strat_aviation": "Стратегічна авіація",
              "mlrs": "РСЗВ"}
EVENT_RADIUS_KM = {"kab": 30.0, "mlrs": 25.0, "missile": 80.0, "air_missile": 80.0, "coastal_missile": 80.0,
                   "aviation": 60.0, "tac_aviation": 60.0, "strat_aviation": 150.0}
EVENT_MAX_AGE_S = 20 * 60
EVENT_AGE_BY_WEAPON = {"kab": 6 * 60, "mlrs": 6 * 60}   # КАБ/РСЗО не «летять»: через кілька хвилин вже неактуально
EVENT_RENOTIFY_S = 30 * 60


def event_key(e: dict) -> str:
    return f"ev:{e['weapon']}:{e['lat']:.2f}:{e['lon']:.2f}"


def event_risks(events: list[dict], point: tuple[float, float], now: int, radius_km: float) -> list[tuple[dict, float]]:
    """Свіжі події без треку (КАБ, ракети, авіація…) у межах радіуса для цієї зброї. [(подія, км)] від найближчої."""
    out = []
    for e in events:
        if e["weapon"] not in EVENT_NAME or now - e["ts"] > EVENT_AGE_BY_WEAPON.get(e["weapon"], EVENT_MAX_AGE_S):
            continue
        d = haversine_km(point, (e["lat"], e["lon"]))
        if d <= max(radius_km, EVENT_RADIUS_KM[e["weapon"]]):
            out.append((e, round(d, 1)))
    return sorted(out, key=lambda x: x[1])


def event_message(e: dict, dist: float, point: tuple[float, float], now: int, label: str = "", map_url: str = "") -> str:
    side = compass_ua(bearing_deg(point, (e["lat"], e["lon"])))
    age = max(0, (now - e["ts"]) // 60)
    icon = "🟠" if e["weapon"] in ("kab", "missile", "air_missile", "coastal_missile", "mlrs") else "🟡"
    text = (f"{icon} {EVENT_NAME[e['weapon']]}: {dist:g} км, {side} від вас, {age} хв тому ({e['place']}). "
            "Курс невідомий, орієнтовно — за зведеннями каналів. Будьте в укритті, якщо тривога.")
    if label:
        text = f"📍 {label}\n{text}"
    return text + (f"\n🗺 {map_url.rstrip('/')}/radar" if map_url else "")


# --- Офіційні тривоги по районах (alerts.in.ua): другий сигнал, коли канали ще мовчать ---------------
from raion_geo import raion_at  # noqa: E402,F401


def official_message(level: str, name: str, label: str = "") -> str:
    if level == "red":
        text = (f"🔴 Тривога у вашому районі ({name} р-н). Офіційні дані. Ціль може бути ще без координат у зведеннях — "
                "негайно в укриття, не чекайте прогнозу.")
    else:
        text = f"🟡 Попередження: загроза БпЛА у вашому районі ({name} р-н). Офіційні дані. Будьте готові піти в укриття."
    return f"📍 {label}\n{text}" if label else text


REPEATS = 2
REPEAT_EVERY_S = 45


def guard_targets(db, now: int) -> list[dict]:
    """Усі точки сторожа: поточна геопозиція/точка + збережені місця. suffix відокремлює стан кожної точки."""
    import guard_places
    out = []
    for g in db.active_guards(now):
        out.append({"uid": g["user_id"], "point": (g["lat"], g["lon"]), "radius": float(g["radius_km"] or 10.0), "label": "", "suffix": ""})
    for p in guard_places.all_places(db):
        out.append({"uid": p["user_id"], "point": (p["lat"], p["lon"]), "radius": float(p["radius_km"] or 10.0),
                    "label": p["name"], "suffix": f"|p{p['id']}"})
    return out


async def evaluate_once(db, send, now: int | None = None, map_url: str = "") -> int:
    """Один прохід. send(user_id, text) — корутина. Возвращает число отправленных сообщений."""
    now = now if now is not None else int(time.time())
    targets = guard_targets(db, now)
    if not targets:
        return 0
    rows = db.recent_observations(now - OBS_WINDOW_S)
    tracks = build_tracks(rows, now, window_s=OBS_WINDOW_S)
    live = {t.key: t for t in tracks if t.status != "stale"}
    events = build_events(rows, now, window_s=EVENT_MAX_AGE_S)
    sent = 0
    import guard_places
    import family
    persistent = guard_places.persistent_users(db)
    try:
        official = set(db.siren_states_fresh())
    except Exception:  # noqa: BLE001
        official = set()
    import os
    official_known = official if os.getenv("ALERTS_IN_UA_TOKEN", "").strip() else None   # None — офіційних даних немає, не заспокоюємо й не лякаємо
    for tg in targets:
        uid, suffix, label = tg["uid"], tg["suffix"], tg["label"]
        risks = risk_for_point(list(live.values()), tg["point"], now, imminent_km=tg["radius"], watch_km=tg["radius"] * 3, official=official_known)
        all_states = db.guard_states(uid)
        # Стан цієї точки: ключі трека + suffix (для живої точки — ключі без «|p»).
        states = {(k[: -len(suffix)] if suffix else k): v for k, v in all_states.items()
                  if (suffix and k.endswith(suffix)) or (not suffix and "|p" not in k)}
        keyed = {}
        for r in risks:
            track = next((t for t in live.values() if t.id == r.track_id), None)
            if track is not None:
                keyed[track.key] = (track, r)
        for key, (track, r) in keyed.items():
            # Ранжування: слабкий сигнал (watch, одне джерело, офіційної тривоги там немає) не будить людину;
            # imminent/over надсилаємо завжди — безпека важливіша за тишу.
            if r.level == "watch" and r.official is False and r.sources < 2:
                continue
            if decide(states.get(key), r, now):
                try:
                    await send(uid, guard_message(r, map_url, label))
                    sent += 1
                except Exception:  # noqa: BLE001 — один недоступный пользователь не должен ронять цикл
                    logger.warning("guard: не удалось отправить %s", uid, exc_info=True)
                    continue
                db.set_guard_state(uid, key + suffix, r.level, r.distance_km, now)
                if r.level in ("imminent", "over") and not family.is_safe(db, uid, now):
                    # Родина: коротке «небезпечно біля <місце>» без координат.
                    await family.notify_members(db, send, uid, family.danger_text(db, uid, label, risk_text(r)))
            elif uid in persistent and r.level in ("imminent", "over") and not family.is_safe(db, uid, now):
                # «Наполегливий режим»: до REPEATS повторів небезпечного сповіщення з інтервалом REPEAT_EVERY_S — кожне дає звук.
                reps = [k for k in states if k.startswith(key + "#r")]
                prev = states.get(key)
                last = max([states[k]["sent_ts"] for k in reps] + ([int(prev["sent_ts"])] if prev else [0]))
                if len(reps) < REPEATS and now - last >= REPEAT_EVERY_S:
                    try:
                        await send(uid, "🔁 ПОВТОР — ще небезпечно\n" + guard_message(r, map_url, label))
                        sent += 1
                    except Exception:  # noqa: BLE001
                        logger.warning("guard: повтор не надіслано %s", uid, exc_info=True)
                        continue
                    db.set_guard_state(uid, f"{key}#r{len(reps) + 1}{suffix}", r.level, r.distance_km, now)
        # Офіційна тривога в районі точки (alerts.in.ua): відразу, навіть коли канали ще не дали координат.
        ra = raion_at(*tg["point"])
        if ra is not None:
            cur = "red" if f"raion:{ra[0]}" in official else ("yellow" if f"raionY:{ra[0]}" in official else None)
            prev = states.get("off:raion")
            prev_lvl = None if prev is None else prev["level"]
            if cur and (prev_lvl is None or (prev_lvl == "watch" and cur == "red")):
                try:
                    await send(uid, official_message(cur, ra[1], label))
                    sent += 1
                    db.set_guard_state(uid, "off:raion" + suffix, "imminent" if cur == "red" else "watch", 0.0, now)
                except Exception:  # noqa: BLE001
                    logger.warning("guard: офіційну тривогу не надіслано %s", uid, exc_info=True)
            elif not cur and prev is not None:
                try:
                    await send(uid, (f"📍 {label}\n" if label else "") + f"🟢 Відбій тривоги у вашому районі ({ra[1]} р-н). Слідкуємо далі.")
                    sent += 1
                except Exception:  # noqa: BLE001
                    logger.warning("guard: відбій району не надіслано %s", uid, exc_info=True)
                db.clear_guard_state(uid, "off:raion" + suffix)
        # Події без треку (КАБ, ракети, авіація…): одне попередження на подію.
        ev_now = {}
        for e, d in event_risks(events, tg["point"], now, tg["radius"]):
            ev_now[event_key(e)] = (e, d)
        for key, (e, d) in ev_now.items():
            prev = states.get(key)
            if prev is not None and now - int(prev["sent_ts"]) < EVENT_RENOTIFY_S:
                continue
            try:
                await send(uid, event_message(e, d, tg["point"], now, label, map_url))
                sent += 1
            except Exception:  # noqa: BLE001
                logger.warning("guard: подію не надіслано %s", uid, exc_info=True)
                continue
            db.set_guard_state(uid, key + suffix, "imminent", d, now)
        for key in [k for k in states if k.startswith("ev:") and k not in ev_now]:
            db.clear_guard_state(uid, key + suffix)
        for key, st in states.items():
            if key.startswith(("ev:", "off:")):
                continue
            if key in keyed or key.split("#r")[0] in keyed:   # «#r…» — лічильник повторів, живе, поки жива ціль
                continue
            track = live.get(key)
            if track is not None and LEVEL_RANK.get(st["level"], 0) >= LEVEL_RANK["imminent"]:
                try:
                    await send(uid, retreat_message(track, label))
                    sent += 1
                except Exception:  # noqa: BLE001
                    logger.warning("guard: не удалось отправить %s", uid, exc_info=True)
            db.clear_guard_state(uid, key + suffix)
    return sent


async def proximity_loop(db, bot_client, interval: int = 20, map_url: str = "") -> None:
    from telethon import Button

    async def send(uid: int, text: str) -> None:
        # Під небезпечними повідомленнями — кнопка «Я в укритті» (зупиняє повтори й сповіщає родину).
        danger = text.split("\n", 1)[-1].startswith(("🟠", "🔴", "🔁")) if text.startswith("📍") else text.startswith(("🟠", "🔴", "🔁"))
        await bot_client.send_message(uid, text, silent=False,
                                      buttons=[[Button.inline("✅ Я в укритті", data="fam:safe")]] if danger else None)
        import push_fcm
        await push_fcm.push_user(db, uid, "🚨 Небезпека поруч" if danger else "AirRadar", text, "danger" if danger else "info")

    while True:
        try:
            await evaluate_once(db, send, map_url=map_url)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("proximity_loop: сбой прохода")
        await asyncio.sleep(interval)
