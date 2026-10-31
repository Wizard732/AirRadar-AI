"""admin_ui.py — админ-команды (ТЗ раздел 5.4 + управление админами).

Команды:
  /status          — здоровье каналов: модули, ошибки, активность
  /give_admin <id|@username> — выдать админку (только супер-админ)
  /analyze         — ИИ-анализ всей системы
  /revoke_admin <id> — забрать админку
  /admins          — список админов
  /ban_channel /unban_channel — модерация каналов

Иерархия: ADMIN_ID из .env = супер-админ (неудаляемый). Остальные — через БД.
Все модули (bot_ui, interests_ui) должны использовать db.is_admin(user_id, super).
"""

from __future__ import annotations

import logging
import time

from telethon import TelegramClient, events

from database import Database

logger = logging.getLogger(__name__)


def register_admin_handlers(
    bot: TelegramClient, db: Database, admin_id: int
) -> None:
    """Навесить админ-команды на bot-клиента.

    admin_id: супер-админ (ADMIN_ID из .env). Только он может /give_admin.
    """
    def _is_super(user_id: int) -> bool:
        return user_id == admin_id

    def _is_admin(user_id: int) -> bool:
        return db.is_admin(user_id, admin_id)

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/status"))
    async def _status(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        await event.respond(_status_text(db), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/summary"))
    async def _summary(event) -> None:  # noqa: ANN001
        """Ручной запуск сводки дня + отправка в канал."""
        if not _is_admin(event.sender_id):
            return
        from digest import _send_evening_forecasts
        from main import _get_publisher
        publisher = _get_publisher()
        n = await _send_evening_forecasts(bot, db, publisher)
        await event.respond(f"✅ Сводка отправлена: {n} получателям + канал.", parse_mode="html")

    async def _resolve_user(ref: str) -> int | None:
        """ID або @username → числовий Telegram ID (юзернейм резолвиться через Telegram)."""
        ref = ref.strip()
        if ref.lstrip("-").isdigit():
            return int(ref)
        try:
            ent = await bot.get_entity(ref if ref.startswith("@") else "@" + ref)
            return int(ent.id)
        except Exception:  # noqa: BLE001
            return None

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/give_admin\s+(\S+)"))
    async def _give_admin(event) -> None:  # noqa: ANN001
        if not _is_super(event.sender_id):
            await event.respond("⛔ Тільки супер-адмін може видавати права.")
            return
        target = await _resolve_user(event.pattern_match.group(1))
        if target is None:
            await event.respond("⛔ Не знайшов користувача. Дай числовий ID або @username "
                                "(за юзернеймом людина має хоч раз написати боту).")
            return
        db.add_admin(target, event.sender_id)
        await event.respond(f"✅ Користувач <code>{target}</code> тепер адмін: усі звіти приходитимуть і йому.", parse_mode="html")
        try:
            await bot.send_message(target, "👑 Тебе призначено адміністратором AirRadar. Тобі приходитимуть службові звіти.")
        except Exception:  # noqa: BLE001 — людина ще не відкривала бота
            pass

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/reports"))
    async def _reports(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        import moderation
        await event.respond(moderation.queue_text(db), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/report_(ok|no)\s+(\d+)"))
    async def _report_decide(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        import moderation
        ok = moderation.resolve(db, int(event.pattern_match.group(2)),
                                "confirmed" if event.pattern_match.group(1) == "ok" else "dismissed")
        await event.respond("✅ Збережено." if ok else "⛔ Звіт не знайдено або вже закритий.")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/fix\s+(\S+)\s+(region|type)\s+(\S+)"))
    async def _fix(event) -> None:  # noqa: ANN001
        """Виправити область/тип події: /fix <ключ> region|type <значення>. Правка стає прикладом для оцінки."""
        if not _is_admin(event.sender_id):
            return
        import moderation
        ok, msg = moderation.apply_fix(db, event.sender_id, event.pattern_match.group(1),
                                       event.pattern_match.group(2), event.pattern_match.group(3))
        await event.respond(msg)

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/stale\s+(\S+)"))
    async def _stale(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        import moderation
        ok = moderation.mark_stale(db, event.pattern_match.group(1))
        await event.respond("✅ Позначено застарілою." if ok else "⛔ Активної події з таким ключем немає.")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/export_reports"))
    async def _export_reports(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        import moderation
        data = moderation.export_confirmed(db)
        await event.respond(("<pre>" + data[:3500] + "</pre>") if data else "Підтверджених помилок поки немає.", parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/health"))
    async def _health(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        import health_checks
        await event.respond(health_checks.health_text(db), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/selftest"))
    async def _selftest(event) -> None:  # noqa: ANN001
        """Синтетична перевірка: ізольована, відповідь лише тому, хто запитав (адмін)."""
        if not _is_admin(event.sender_id):
            return
        import health_checks
        r = health_checks.synthetic_check()
        ok = r["safety_policy"] and r["isolated"]
        probe = await health_checks.delivery_probe(db, bot, event.sender_id)
        android = "не прив'язано" if probe["android"] is None else ("✅" if probe["android"] else "🔴")
        await event.respond(f"{'✅' if ok else '🔴'} Синтетична перевірка: safety_policy={r['safety_policy']}, "
                            f"важливість={r['importance']}, ізоляція від підписників={r['isolated']}\n"
                            f"Доставка (лише вам): Telegram {'✅' if probe['telegram'] else '🔴'}, Android/ntfy {android}\n"
                            "Тест не потрапляє в канал і до підписників.")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/preview"))
    async def _preview(event) -> None:  # noqa: ANN001
        """Статичне превʼю карти (офіційні тривоги) — лише адміну, для перевірки перед публікацією."""
        if not _is_admin(event.sender_id):
            return
        import map_preview
        from kyiv_time import fmt
        regs = set(db.siren_states_fresh(900))
        png = map_preview.render_png(regs)
        await bot.send_file(event.sender_id, png, caption=map_preview.caption(regs, fmt(int(time.time()))),
                            file_name="airradar.png", force_document=False)

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/analyze"))
    async def _analyze(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        from ai_analyzer import analyze
        try:
            from main import _get_summarizer
            summarizer = _get_summarizer()
        except Exception:  # noqa: BLE001
            summarizer = None
        await event.respond("🧠 Аналізую систему…")
        await event.respond(await analyze(db, summarizer), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/revoke_admin\s+(\d+)"))
    async def _revoke_admin(event) -> None:  # noqa: ANN001
        if not _is_super(event.sender_id):
            await event.respond("⛔ Только супер-админ может забирать права.")
            return
        target = int(event.pattern_match.group(1))
        if target == admin_id:
            await event.respond("⛔ Супер-админа нельзя удалить.")
            return
        db.remove_admin(target)
        await event.respond(f"🚫 У пользователя <code>{target}</code> забрана админка.", parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/admins"))
    async def _admins(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        admins = db.all_admins()
        lines = [f"👤 <b>Администраторы</b>\n", f"👑 Супер-админ: <code>{admin_id}</code>"]
        if admins:
            lines.append("\nДобавленные:")
            for uid in admins:
                lines.append(f"• <code>{uid}</code>")
        else:
            lines.append("\nДобавленных админов нет.")
        lines.append(f"\n/give_admin &lt;id|@username&gt; — выдать (только супер)")
        lines.append("/revoke_admin &lt;id&gt; — забрать")
        lines.append("/analyze — ІІ-аналіз системи")
        await event.respond("\n".join(lines), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/review(?:\s+(\d+))?$"))
    async def _review(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        limit = int(event.pattern_match.group(1) or 10)
        rows = db.review_queue(min(limit, 30))
        if not rows:
            await event.respond("✅ Очередь проверки пуста.")
            return
        lines = ["🔎 <b>Очередь проверки</b>"]
        for row in rows:
            lines.append(f"• <code>{row['incident_key']}</code> | {row['region']} | {row['weapon_class']} | {row['source_count']} ист. | {row['status']}")
        lines.append("\n/approve <key> <причина> | /reject <key> <причина>")
        await event.respond("\n".join(lines), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/(approve|reject)\s+(\S+)\s+(.+)$"))
    async def _review_decision(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        action, key, reason = event.pattern_match.group(1), event.pattern_match.group(2), event.pattern_match.group(3)
        decision = "officially_confirmed" if action == "approve" else "retracted"
        if db.review_incident(key, decision, reason, event.sender_id):
            await event.respond("✅ Решение сохранено.")
        else:
            await event.respond("⛔ Не удалось сохранить: нужна непустая причина и корректный статус.")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/ban_channel\s+(\S+)"))
    async def _ban(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channel = event.pattern_match.group(1).strip()
        db.disable_channel(channel)
        await event.respond(f"🚫 Канал <code>{channel}</code> заблокирован.", parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/unban_channel\s+(\S+)"))
    async def _unban(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channel = event.pattern_match.group(1).strip()
        db.enable_channel(channel)
        await event.respond(f"✅ Канал <code>{channel}</code> разблокирован.", parse_mode="html")


def _status_text(db: Database) -> str:
    """Сводка здоровья для /status: каналы + конвейер + сирены."""
    rows = db.all_channel_health()
    if not rows:
        return "📋 <b>Статус</b>\n\nДанных о каналах пока нет."
    lines = ["📋 <b>Статус каналов</b>\n"]
    disabled_n = 0
    errors_n = 0
    for r in rows:
        ch = r["channel"]
        mod = r["module"]
        err = r["error_count"]
        dis = r["disabled"]
        mark = "🚫" if dis else ("⚠️" if err > 0 else "✅")
        if dis:
            disabled_n += 1
        if err > 0:
            errors_n += 1
        seen = r["last_seen"]
        ago = f"{(int(time.time()) - seen) // 60} мин" if seen else "—"
        lines.append(f"{mark} <code>{ch}</code> [{mod}] errs={err} last={ago}")
    lines.append(f"\nВсего: {len(rows)} | ⚠️ с ошибками: {errors_n} | 🚫 забанено: {disabled_n}")

    # Задержка конвейера «пост источника → пост в канале» за 24ч (p50/p95).
    try:
        lat = db.latency_percentiles(seconds=86400)
        if lat.get("samples"):
            lines.append(
                f"\n⏱ Конвейер (24г): p50 <b>{lat['p50_ms'] / 1000:.1f} с</b> · "
                f"p95 <b>{lat['p95_ms'] / 1000:.1f} с</b> "
                f"({lat['samples']} замеров)"
            )
        else:
            lines.append("\n⏱ Конвейер (24г): замеров пока нет")
    except Exception:  # noqa: BLE001 — метрика не роняет /status
        pass

    # Официальные сирены (alerts.in.ua): активные, если данные свежие (≤15 мин).
    try:
        fresh = db.siren_states_fresh(max_age_sec=900)
        if fresh:
            names = ", ".join(fresh[:8])
            if len(fresh) > 8:
                names += f" +{len(fresh) - 8}"
            lines.append(f"🚨 Официальные сирены ({len(fresh)}): {names}")
        elif db.siren_states_active():
            lines.append("🚨 Сирены: данные опроса устарели (>15 мин) — проверь ALERTS_IN_UA_TOKEN")
    except Exception:  # noqa: BLE001
        pass

    lines.append("\n/ban_channel @name — заблокировать")
    lines.append("/unban_channel @name — разблокировать")
    return "\n".join(lines)
