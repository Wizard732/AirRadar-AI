"""interests_ui.py — меню Interests-модуля (inline-кнопки).

Главное меню Interests:
  📰 Темы → список 15 тем, кнопки подписки (🔔/🔕)
  📡 Мои каналы → список добавленных user-каналов
  ➕ Добавить канал — подсказка про команду /add_channel
  🏠 Главное меню — возврат к выбору модуля

Команды (текстом):
  /add_channel @username — подписать аккаунт на канал для парсинга
  /my_channels — список каналов
"""

from __future__ import annotations

import logging

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


def _interests_main_kb() -> list:
    return [
        [Button.inline("📰 Темы и подписки", data=CB_INT_TOPICS)],
        [Button.inline("📡 Мои каналы", data=CB_INT_CHANNELS)],
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
        lines.append(f"• <code>{ch}</code>")
    lines.append("\nДобавить ещё: <code>/add_channel @username</code>")
    return "\n".join(lines)


def register_interests_handlers(bot: TelegramClient, db: Database, admin_id: int) -> None:
    """Навесить обработчики команд и кнопок Interests-модуля."""

    def _is_admin(user_id: int) -> bool:
        return user_id == admin_id

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/add_channel\s+(\S+)"))
    async def _add_channel(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        channel = event.pattern_match.group(1).strip()
        db.add_user_channel(event.sender_id, channel)
        await event.respond(
            f"✅ Канал <code>{channel}</code> добавлен. Бот начнёт анализировать его посты.",
            parse_mode="html",
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("Interests UI: ошибка callback %s: %s", data, exc)
            await event.answer("Ошибка, смотри логи.", alert=True)
