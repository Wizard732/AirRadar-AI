"""interests_ui.py — меню Interests-модуля (inline-кнопки).

Главное меню Interests:
  📰 Темы → список 15 тем, кнопки подписки (🔔/🔕)
  📡 Мои каналы → список добавленных user-каналов
  🔍 Мои интересы → свободные интересы (семантический поиск)
  🏠 Главное меню — возврат к выбору модуля

Команды (текстом):
  /add_channel @username — подписать аккаунт на канал для парсинга
  /my_channels — список каналов
  /del_channel @username — удалить канал
"""

from __future__ import annotations

import html
import logging
import re

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

from database import Database
from interests_config import TOPICS, topic_label

logger = logging.getLogger(__name__)

# Callback-префиксы Interests-модуля.
CB_INT_MAIN = "im:"        # меню Interests
CB_INT_TOPICS = "it:"      # список тем
CB_INT_TOPIC_TOGGLE = "itt:"  # подписка на тему: itt:crypto
CB_INT_CHANNELS = "ic:"    # мои каналы

PAGE_SIZE = 8

# Валидное имя канала: буква в начале, 4–64 символа [A-Za-z0-9_].
_CHANNEL_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,63}$")


def normalize_channel(raw: str) -> str | None:
    """Привести ввод канала к '@username' или вернуть None, если невалидно.

    Принимает: @username, username, https://t.me/username, t.me/username.
    """
    value = (raw or "").strip()
    # Ссылки t.me / telegram.me (с схемой и без).
    link = re.match(r"^(?:https?://)?t(?:elegram)?\.me/([A-Za-z][A-Za-z0-9_]{3,63})/?$", value, re.IGNORECASE)
    if link:
        return "@" + link.group(1)
    if _CHANNEL_RE.match(value):
        return value if value.startswith("@") else "@" + value
    return None


def _interests_main_kb() -> list:
    return [
        [Button.inline("📰 Темы и подписки", data=CB_INT_TOPICS)],
        [Button.inline("📡 Мои каналы", data=CB_INT_CHANNELS)],
        [Button.inline("🔍 Мои интересы", data="interests_list")],
        [Button.inline("🏠 Главное меню", data="main")],
    ]


def _topics_kb(user_topics: dict, page: int = 0) -> list:
    """Сетка тем с пагинацией. user_topics: {slug: mode} (mode='instant'|'digest').

    Метки: 🔔 — не подписан (клик=мгновенно), ⚡ — instant (клик=дайджест),
    🕗 — digest (клик=отписка).
    """
    start = page * PAGE_SIZE
    chunk = TOPICS[start : start + PAGE_SIZE]
    rows = []
    for slug, label in chunk:
        mode = user_topics.get(slug)
        mark = "🔔" if mode is None else ("🕗" if mode == "digest" else "⚡")
        rows.append([Button.inline(f"{mark} {label}", data=CB_INT_TOPIC_TOGGLE + slug)])
    # Навигация.
    total_pages = (len(TOPICS) + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️", data=f"{CB_INT_TOPICS}{page - 1}"))
    nav.append(Button.inline("⬅️ Назад", data=CB_INT_MAIN))
    if page + 1 < total_pages:
        nav.append(Button.inline("▶️", data=f"{CB_INT_TOPICS}{page + 1}"))
    rows.append(nav)
    return rows


def _interests_main_text() -> str:
    return (
        "📰 <b>Новости по интересам</b>\n\n"
        "Бот анализирует посты из добавленных тобой каналов и присылает в ЛС "
        "только то, что подходит под выбранные темы.\n\n"
        "• <b>Темы</b> — выбери, что интересует. Кнопки: 🔔 подписаться мгновенно, ⚡ дайджест, 🕗 отписка\n"
        "• <b>Мои каналы</b> — источники новостей\n"
        "• Добавить канал: команда <code>/add_channel @username</code>"
    )


def _topics_text() -> str:
    return "📰 <b>Выбери темы</b>\n\n🔔 — подписаться, 🔕 — отписаться. Листай ◀️ ▶️."


def _channels_text(channels: list[str]) -> str:
    if not channels:
        return (
            "📡 <b>Мои каналы</b>\n\n"
            "Пока нет добавленных каналов.\n"
            "Добавить: <code>/add_channel @username</code>"
        )
    lines = ["📡 <b>Мои каналы</b>\n"]
    for ch in channels:
        lines.append(f"• <code>{ch}</code> — удалить: <code>/del_channel {ch}</code>")
    lines.append("\nДобавить ещё: <code>/add_channel @username</code>")
    return "\n".join(lines)


def register_interests_handlers(bot: TelegramClient, db: Database, admin_id: int) -> None:
    """Навесить обработчики команд и кнопок Interests-модуля."""

    def _is_admin(user_id: int) -> bool:
        # Супер-админ (из .env) ИЛИ добавленный через /give_admin (из БД).
        return db.is_admin(user_id, admin_id)

    # --- Семантический поиск: свободные интересы (ТЗ 5.3) ---
    @bot.on(events.NewMessage(incoming=True, pattern=r"^/add_interest\s+(.+)"))
    async def _add_interest(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        interest = event.pattern_match.group(1).strip()
        db.add_interest(event.sender_id, interest)
        await event.respond(
            f"🔍 Добавлен интерес: <b>{interest}</b>\nБот будет присылать посты по смыслу.",
            parse_mode="html",
        )

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/del_interest\s+(.+)"))
    async def _del_interest(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        interest = event.pattern_match.group(1).strip()
        db.remove_interest(event.sender_id, interest)
        await event.respond(f"🗑 Интерес «{interest}» удалён.", parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/my_interests"))
    async def _my_interests(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        interests = db.user_interests(event.sender_id)
        if not interests:
            await event.respond(
                "🔍 <b>Мои интересы</b>\n\nПока пусто. Добавь: <code>/add_interest фьюжн-реакторы</code>",
                parse_mode="html",
            )
            return
        lines = ["🔍 <b>Мои интересы</b>\n"]
        for i in interests:
            lines.append(f"• <i>{i}</i>")
        lines.append("\n/add_interest … — добавить")
        lines.append("/del_interest … — удалить")
        await event.respond("\n".join(lines), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/add_channel(?:\s+(.+))?$"))
    async def _add_channel(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw:
            await event.respond(
                "📡 <b>Добавление канала</b>\n\n"
                "Формат: <code>/add_channel @username</code>\n"
                "Принимаются также <code>t.me/username</code> и просто <code>username</code>.",
                parse_mode="html",
            )
            return
        channel = normalize_channel(raw)
        if channel is None:
            await event.respond(
                f"⚠️ <code>{html.escape(raw)}</code> не похоже на имя канала.\n"
                "Правильно: <code>/add_channel @username</code> "
                "(только латиница, цифры и _; публичный канал).",
                parse_mode="html",
            )
            return
        if channel in db.user_channels(event.sender_id):
            await event.respond(
                f"ℹ️ Канал <code>{channel}</code> уже добавлен.",
                parse_mode="html",
            )
            return
        db.add_user_channel(event.sender_id, channel)
        await event.respond(
            f"✅ Канал <code>{channel}</code> добавлен. Посты начнут приходить, "
            f"когда бот-аккаунт увидит канал (обычно сразу; максимум — пару минут).\n"
            f"Удалить: <code>/del_channel {channel}</code>",
            parse_mode="html",
        )

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/del_channel\s+(\S+)"))
    async def _del_channel(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        raw = event.pattern_match.group(1).strip()
        channel = normalize_channel(raw)
        channels = db.user_channels(event.sender_id)
        target = channel if channel in channels else raw
        if target not in channels:
            await event.respond(
                f"ℹ️ Канала <code>{html.escape(raw)}</code> нет в твоём списке.",
                parse_mode="html",
            )
            return
        db.remove_user_channel(event.sender_id, target)
        await event.respond(
            f"🗑 Канал <code>{target}</code> удалён.", parse_mode="html"
        )

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/my_channels"))
    async def _my_channels(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channels = db.user_channels(event.sender_id)
        await event.respond(_channels_text(channels), parse_mode="html")

    @bot.on(events.CallbackQuery())
    async def _callback(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            await event.answer("Нет доступа.", alert=True)
            return
        data = event.data.decode("utf-8") if isinstance(event.data, bytes) else event.data

        async def _safe_edit(text: str, buttons) -> None:
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
            except MessageNotModifiedError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("Interests UI: не обновить: %s", exc)

        try:
            if data == CB_INT_MAIN:
                await _safe_edit(_interests_main_text(), _interests_main_kb())

            elif data == CB_INT_TOPICS or data.startswith(CB_INT_TOPICS):
                # it: или it:N (страница)
                page = int(data[len(CB_INT_TOPICS):] or "0")
                user_topics = db.user_topics_with_modes(event.sender_id)
                await _safe_edit(_topics_text(), _topics_kb(user_topics, page))

            elif data.startswith(CB_INT_TOPIC_TOGGLE):
                slug = data[len(CB_INT_TOPIC_TOGGLE):]
                if slug in {s for s, _ in TOPICS}:
                    if db.is_subscribed_topic(event.sender_id, slug):
                        mode = db.topic_delivery_mode(event.sender_id, slug)
                        if mode == "instant":
                            # instant → digest (следующий клик переключит режим)
                            db.subscribe_topic(event.sender_id, slug, mode="digest")
                            await event.answer("🕗 Дайджест (утро/вечер): " + topic_label(slug))
                        elif mode == "digest":
                            # digest → отписка
                            db.unsubscribe_topic(event.sender_id, slug)
                            await event.answer("🔕 Отписка: " + topic_label(slug))
                    else:
                        # не подписан → instant
                        db.subscribe_topic(event.sender_id, slug, mode="instant")
                        await event.answer("🔔 Мгновенно: " + topic_label(slug))
                    user_topics = db.user_topics_with_modes(event.sender_id)
                    await _safe_edit(_topics_text(), _topics_kb(user_topics))

            elif data == CB_INT_CHANNELS:
                channels = db.user_channels(event.sender_id)
                await _safe_edit(
                    _channels_text(channels),
                    [[Button.inline("⬅️ Назад", data=CB_INT_MAIN)]],
                )

            elif data == "interests_list":
                interests = db.user_interests(event.sender_id)
                if interests:
                    lines = ["🔍 <b>Мои интересы</b>\n"]
                    for i in interests:
                        lines.append(f"• <i>{i}</i>")
                    lines.append("\n/del_interest … — удалить")
                else:
                    lines = [
                        "🔍 <b>Мои интересы</b>\n",
                        "Пока пусто. Добавь команду:",
                        "<code>/add_interest фьюжн-реакторы</code>",
                    ]
                await _safe_edit(
                    "\n".join(lines),
                    [[Button.inline("⬅️ Назад", data=CB_INT_MAIN)]],
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Interests UI: ошибка callback %s: %s", data, exc)
            await event.answer("Ошибка, смотри логи.", alert=True)
