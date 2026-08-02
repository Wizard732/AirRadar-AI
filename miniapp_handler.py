"""miniapp_handler.py — приём данных из Telegram Mini App.

Mini App (miniapp/index.html) шлёт изменения подписок/каналов через
Telegram.WebApp.sendData(). На стороне бота это приходит как обычное
сообщение (event.message.text) с JSON-пайлоадом от пользователя.

Этот модуль парсит JSON-команды (set_topic / add_channel / remove_channel)
и применяет их к БД. Регистрируется на bot-клиенте.
"""

from __future__ import annotations

import json
import logging

from telethon import TelegramClient, events

from database import Database

logger = logging.getLogger(__name__)


def register_miniapp_handlers(bot: TelegramClient, db: Database, admin_id: int) -> None:
    """Навесить обработчик данных из Mini App.

    Ловит сообщения, начинающиеся с '{"action":' — это sendData из WebApp.
    """

    @bot.on(events.NewMessage(incoming=True, func=lambda e: _is_miniapp_data(e)))
    async def _handle(event) -> None:  # noqa: ANN001
        # Только админы могут менять свои подписки через Mini App.
        if not db.is_admin(event.sender_id, admin_id):
            return
        try:
            data = json.loads(event.message.text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("MiniApp: невалидный JSON от %s", event.sender_id)
            return

        action = data.get("action")
        try:
            if action == "set_topic":
                topic = data.get("topic", "")
                mode = data.get("mode", "off")
                if mode == "off":
                    db.unsubscribe_topic(event.sender_id, topic)
                else:
                    db.subscribe_topic(event.sender_id, topic, mode=mode)
                logger.info("MiniApp: %s set_topic %s=%s", event.sender_id, topic, mode)

            elif action == "add_channel":
                channel = data.get("channel", "").strip()
                if channel:
                    db.add_user_channel(event.sender_id, channel)
                    logger.info("MiniApp: %s add_channel %s", event.sender_id, channel)

            elif action == "remove_channel":
                channel = data.get("channel", "").strip()
                if channel:
                    db.remove_user_channel(event.sender_id, channel)
                    logger.info("MiniApp: %s remove_channel %s", event.sender_id, channel)

            else:
                logger.warning("MiniApp: неизвестное действие %s от %s", action, event.sender_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("MiniApp: ошибка обработки: %s", exc)


def _is_miniapp_data(event) -> bool:  # noqa: ANN001
    """Признак: сообщение пришло из WebApp (JSON с ключом 'action')."""
    text = getattr(getattr(event, "message", None), "text", "") or ""
    return text.startswith('{"action"')
