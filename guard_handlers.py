"""guard_handlers.py — «🛡 Гео-сторож»: команди бота и приём live-локации.

Пользователь включает сторож (/guard on), шлёт Live Location в чат с ботом
(скрепка → Локація → «Транслювати мою геопозицію») — Telegram сам присылает
обновления позиции, и бот в реальном времени сверяет её с треками целей.
Регистрируется ДО обработчиков «Моя зона» и geo-репорта: локация включённого
сторожа не должна становиться публичным репортом об угрозе на карте.
"""

from __future__ import annotations

import html
import logging

from telethon import TelegramClient, events, types

from database import Database

logger = logging.getLogger(__name__)

RADII = (5, 10, 15, 25)

GUARD_HELP = (
    "🛡 <b>Гео-сторож</b>\n\n"
    "Бот стежить за цілями відносно твоєї точки й пише одразу, коли шахед "
    "наближається (за замовчуванням ≤10 км і курс на тебе).\n\n"
    "<b>Як увімкнути</b>\n"
    "1. <code>/guard on</code>\n"
    "2. Надішли live-геопозицію: 📎 → Локація → «Транслювати мою геопозицію» "
    "(до 8 годин). Або звичайну точку — тоді сторож стежитиме за нею.\n\n"
    "<b>Налаштування</b>: <code>/guard 5</code> · <code>/guard 10</code> · "
    "<code>/guard 15</code> · <code>/guard 25</code> — радіус у км.\n"
    "<code>/guard off</code> — вимкнути й стерти координати.\n\n"
    "Дані орієнтовні: канали публікують зведення, а не радарні координати."
)


def _status(g: dict | None) -> str:
    if not g or not g["enabled"]:
        return "🔕 Сторож вимкнено."
    where = "очікую геопозицію" if g["lat"] is None else (
        "live-геопозиція" if g["live"] else "фіксована точка")
    return f"🛡 Сторож увімкнено · радіус {g['radius_km']:g} км · {where}."


def register_guard_handlers(bot: TelegramClient, db: Database) -> None:
    import time as _time

    import menu_screens
    if not hasattr(bot, "_airradar_city_pending"):
        bot._airradar_city_pending = {}

    if not hasattr(bot, "_airradar_pending"):
        bot._airradar_pending = {}

    def _pending(uid):  # noqa: ANN001
        p = bot._airradar_pending.get(uid)
        return p if p and p[2] > _time.time() else None

    def _awaiting_city(e) -> bool:  # noqa: ANN001
        text = (getattr(getattr(e, "message", None), "text", "") or "").strip()
        if not (bool(getattr(e, "is_private", False)) and bool(text) and not text.startswith("/")):
            return False
        return bot._airradar_city_pending.get(e.sender_id, 0) > _time.time() or _pending(e.sender_id) is not None

    async def _handle_pending_text(event, text: str) -> bool:  # noqa: ANN001
        p = _pending(event.sender_id)
        if p is None:
            return False
        kind, arg, _ = p
        if kind == "place":
            ok, reply = menu_screens.place_add_from_text(db, event.sender_id, arg, text)
            if ok:
                bot._airradar_pending.pop(event.sender_id, None)
            t, rows = menu_screens.places_screen(db, event.sender_id, reply) if ok else (reply, None)
            await event.respond(t, parse_mode="html", buttons=rows)
            return True
        if kind == "shelter":
            from threat_parser import locate
            geo = locate(text.strip(), "")
            if geo is None or geo.precision not in ("place", "approx"):
                await event.respond("Не знайшов такого населеного пункту. Спробуй інакше.")
                return True
            bot._airradar_pending.pop(event.sender_id, None)
            t, rows = await menu_screens.shelter_reply(geo.lat, geo.lon, geo.name)
            await event.respond(t, parse_mode="html", buttons=rows, link_preview=False)
            return True
        return False

    @bot.on(events.NewMessage(incoming=True, func=_awaiting_city))
    async def _city_answer(event) -> None:  # noqa: ANN001
        if await _handle_pending_text(event, event.message.text):
            raise events.StopPropagation
        reply = menu_screens.set_city_guard(db, event.sender_id, event.message.text)
        if reply.startswith("🛡"):
            bot._airradar_city_pending.pop(event.sender_id, None)
        await event.respond(reply, parse_mode="html")
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/pairweb(?:@\w+)?\s+(\S+)\s*$"))
    async def _pairweb(event) -> None:  # noqa: ANN001
        import webpush_push
        ok, msg = webpush_push.pair(db, event.sender_id, event.pattern_match.group(1))
        await event.respond(msg)
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/unpairweb(?:@\w+)?\s*$"))
    async def _unpairweb(event) -> None:  # noqa: ANN001
        import webpush_push
        n = webpush_push.unpair_all(db, event.sender_id)
        await event.respond(f"Web Push вимкнено ({n} пристроїв)." if n else "Підключених пристроїв немає.")
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?\s+ack_(\w+)\s*$"))
    async def _ack_from_push(event) -> None:  # noqa: ANN001
        """Кнопка «Отримав» у пуші Android/ntfy веде сюди: фіксуємо підтвердження отримання."""
        key = event.pattern_match.group(1)
        db.record_delivery(key, f"ntfy:{event.sender_id}", "critical", "acknowledged")
        await event.respond("✅ Дякуємо, відмічено, що ви отримали сповіщення. Бережіть себе!")
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?\s+fam_(\w+)\s*$"))
    async def _family_join(event) -> None:  # noqa: ANN001
        import family
        if not getattr(event, "is_private", True):
            return
        uid = event.sender_id
        try:
            sender = await event.get_sender()
            family.remember_name(db, uid, getattr(sender, "first_name", "") or getattr(sender, "username", "") or "")
        except Exception:  # noqa: BLE001
            pass
        ok, msg, owner = family.accept_invite(db, event.pattern_match.group(1), uid)
        if not ok:
            await event.respond(msg)
            raise events.StopPropagation
        text, rows = menu_screens.family_screen(db, uid, f"✅ Ви тепер у родині з <b>{family.name_of(db, owner)}</b>.")
        await event.respond(text, parse_mode="html", buttons=rows)
        try:
            await bot.send_message(owner, f"👪 {family.name_of(db, uid)} приєднався(лась) до вашої родини. Тепер ви бачите короткі повідомлення одне про одного.")
        except Exception:  # noqa: BLE001
            pass
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/pair(?:@\w+)?(?:\s+(\S+))?\s*$"))
    async def _pair_cmd(event) -> None:  # noqa: ANN001
        import push_fcm
        if not getattr(event, "is_private", True):
            return
        code = event.pattern_match.group(1) or ""
        if not code:
            text, rows = menu_screens.app_screen(db, event.sender_id)
            await event.respond(text, parse_mode="html", buttons=rows, link_preview=False)
        else:
            ok, msg = push_fcm.pair(db, event.sender_id, code)
            await event.respond("✅ Додаток підключено: тривоги та попередження приходитимуть пушем." if ok else msg)
            try:  # код = ключ до пушів; прибираємо повідомлення з кодом із чату
                await event.delete()
            except Exception:  # noqa: BLE001
                pass
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/unpair(?:@\w+)?\s*$"))
    async def _unpair_cmd(event) -> None:  # noqa: ANN001
        import push_fcm
        if not getattr(event, "is_private", True):
            return
        n = push_fcm.unpair_all(db, event.sender_id)
        await event.respond(f"🔌 Відключено пристроїв: {n}" if n else "Прив'язаних пристроїв немає.")
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/home(?:@\w+)?\s+(.+)$"))
    async def _home_cmd(event) -> None:  # noqa: ANN001
        if not getattr(event, "is_private", True):
            return
        await event.respond(menu_screens.set_city_guard(db, event.sender_id, event.pattern_match.group(1)), parse_mode="html")
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/guard(?:@\w+)?(?:\s+(\S+))?\s*$"))
    async def _guard_cmd(event) -> None:  # noqa: ANN001
        if not getattr(event, "is_private", True):
            return
        uid = event.sender_id
        arg = (event.pattern_match.group(1) or "").lower()
        if arg == "on":
            db.set_guard(uid, True)
            await event.respond("🛡 Сторож увімкнено.\n" + GUARD_HELP.split("<b>Як увімкнути</b>")[0].strip()
                                + "\n\nТепер надішли геопозицію (краще live): 📎 → Локація.", parse_mode="html")
        elif arg == "off":
            db.set_guard(uid, False)
            await event.respond("🔕 Сторож вимкнено, координати стерто.")
        elif arg.isdigit() and int(arg) in RADII:
            db.set_guard(uid, True, float(arg))
            await event.respond(f"🛡 Радіус: {arg} км. " + _status(db.get_guard(uid)))
        elif arg:
            await event.respond(f"Радіус: {', '.join(map(str, RADII))} км. Наприклад: <code>/guard 10</code>",
                                parse_mode="html")
        else:
            await event.respond(html.escape(_status(db.get_guard(uid))) + "\n\n" + GUARD_HELP, parse_mode="html")
        raise events.StopPropagation

    def _is_geo(e) -> bool:  # noqa: ANN001
        return getattr(getattr(e, "message", None), "geo", None) is not None and getattr(e, "is_private", False)

    @bot.on(events.NewMessage(incoming=True, func=_is_geo))
    async def _guard_geo(event) -> None:  # noqa: ANN001
        uid = event.sender_id
        p = _pending(uid)
        if p is not None:
            geo_ = event.message.geo
            bot._airradar_pending.pop(uid, None)
            if p[0] == "place":
                import guard_places
                ok, msg = guard_places.add_place(db, uid, p[1] or "Місце", float(geo_.lat), float(geo_.long))
                t, rows = menu_screens.places_screen(db, uid, f"✅ Збережено: <b>{msg}</b>" if ok else msg)
                await event.respond(t, parse_mode="html", buttons=rows)
            else:
                t, rows = await menu_screens.shelter_reply(float(geo_.lat), float(geo_.long), "ваша точка")
                await event.respond(t, parse_mode="html", buttons=rows, link_preview=False)
            raise events.StopPropagation
        g = db.get_guard(uid)
        if not g or not g["enabled"]:
            return  # сторож выключен — работают «Моя зона» и geo-репорт
        geo = event.message.geo
        live = isinstance(getattr(event.message, "media", None), types.MessageMediaGeoLive)
        db.update_guard_location(uid, float(geo.lat), float(geo.long), live)
        await event.respond(
            ("📡 Live-геопозицію підключено. " if live else "📍 Точку збережено. ")
            + _status(db.get_guard(uid)),
        )
        raise events.StopPropagation

    @bot.on(events.MessageEdited(incoming=True, func=_is_geo))
    async def _guard_geo_update(event) -> None:  # noqa: ANN001
        geo = event.message.geo
        live = isinstance(getattr(event.message, "media", None), types.MessageMediaGeoLive)
        db.update_guard_location(event.sender_id, float(geo.lat), float(geo.long), live)
