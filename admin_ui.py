"""admin_ui.py — админ-команды (ТЗ раздел 5.4 + управление админами).

Команды:
  /status          — здоровье каналов: модули, ошибки, активность
  /give_admin <id> — выдать админку (только супер-админ)
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

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/give_admin\s+(\d+)"))
    async def _give_admin(event) -> None:  # noqa: ANN001
        if not _is_super(event.sender_id):
            await event.respond("⛔ Только супер-админ может выдавать права.")
            return
        target = int(event.pattern_match.group(1))
        db.add_admin(target, event.sender_id)
        await event.respond(f"✅ Пользователь <code>{target}</code> теперь админ.", parse_mode="html")

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
        lines.append(f"\n/give_admin <id> — выдать (только супер)")
        lines.append("/revoke_admin <id> — забрать")
        await event.respond("\n".join(lines), parse_mode="html")

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
