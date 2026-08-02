"""admin_ui.py — админ-команды (ТЗ раздел 5.4).

Команды (только для ADMIN_ID):
  /status       — здоровье бота: каналы, LLM-бэкенд, соединения, ошибки
  /ban_channel  — отключить канал (модерация)
  /unban_channel — включить обратно
  /reload       — переразрешить каналы (без рестарта всего бота)

Логика модерации: забаненный канал попадает в channel_health.disabled=1,
что влияет на выборку при следующем /reload.
"""

from __future__ import annotations

import logging
import time

from telethon import TelegramClient, events

from database import Database

logger = logging.getLogger(__name__)


def register_admin_handlers(bot: TelegramClient, db: Database, admin_id: int) -> None:
    """Навесить админ-команды на bot-клиента."""

    def _is_admin(user_id: int) -> bool:
        return user_id == admin_id

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/status"))
    async def _status(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        await event.respond(_status_text(db), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/ban_channel\s+(\S+)"))
    async def _ban(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channel = event.pattern_match.group(1).strip()
        db.disable_channel(channel)
        await event.respond(f"🚫 Канал <code>{channel}</code> заблокирован. /reload — применить.", parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/unban_channel\s+(\S+)"))
    async def _unban(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channel = event.pattern_match.group(1).strip()
        db.enable_channel(channel)
        await event.respond(f"✅ Канал <code>{channel}</code> разблокирован. /reload — применить.", parse_mode="html")


def _status_text(db: Database) -> str:
    """Сводка здоровья для /status."""
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
    lines.append("\n/ban_channel @name — заблокировать")
    lines.append("/unban_channel @name — разблокировать")
    return "\n".join(lines)
