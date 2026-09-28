"""publisher.py — публикация обработанных постов в целевой канал через Bot API.

Использует единый aiohttp-клиент (один ClientSession на всё время жизни приложения)
для запросов к ``https://api.telegram.org/bot<token>/sendMessage``. Ошибки
отправки (нет прав, превышена длина, rate-limit) логируются, но не роняют
конвейер: одно неудачное сообщение не должно стопить весь поток.

К каждому посту прикрепляется inline-кнопка «Підписатися» на наш канал
(promo_url) — пассивная реклама вместо упоминания исходников.
"""

from __future__ import annotations

import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Жёсткий лимит Telegram на длину sendMessage. На практике ИИ-выжимка + заголовок
# укладываются далеко в предел, но подстрахуемся от аномально длинного оригинала
# при fallback'е.
TG_TEXT_LIMIT = 4096


class Publisher:
    """Отправка сообщений в целевой канал через Telegram Bot API."""

    def __init__(
        self,
        bot_token: str,
        target_channel: str,
        timeout: int,
        session: aiohttp.ClientSession | None = None,
        promo_url: str = "",
    ) -> None:
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._target = target_channel
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None
        self._promo_url = promo_url.strip()

    def _promo_markup(self) -> str | None:
        """Inline-кнопка подписки на наш канал (JSON для Bot API)."""
        if not self._promo_url:
            return None
        return json.dumps(
            {"inline_keyboard": [[{"text": "🔔 Підписатися", "url": self._promo_url}]]},
            ensure_ascii=False,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def send(self, text: str) -> dict | None:
        """Отправить текст в целевой канал.

        Возвращает result-объект Telegram при успехе, None/False при ошибке
        (с логированием причины). Скрипт при этом продолжает работать —
        следующее сообщение уйдёт нормально.
        """
        if not text:
            logger.debug("Пустой текст — публикация пропущена.")
            return False

        # Подстраховка от лимита Telegram: режем с конца, заголовок (в начале) важнее.
        if len(text) > TG_TEXT_LIMIT:
            text = text[:TG_TEXT_LIMIT]
            logger.warning("Сообщение обрезано до %d символов перед отправкой.", TG_TEXT_LIMIT)

        # Channel posts and LLM output are untrusted. Plain text prevents a
        # broken `[` or `**` from making Telegram reject a real alert.
        payload = {
            "chat_id": self._target,
            "text": text,
            "disable_web_page_preview": True,
        }
        promo = self._promo_markup()
        if promo:
            payload["reply_markup"] = promo

        try:
            session = await self._get_session()
            async with session.post(self._api_url, json=payload) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    logger.info("Опубликовано в %s (%d символов).", self._target, len(text))
                    return data.get("result") or {"chat": {"id": self._target}, "message_id": 0}

                # Чаще всего: бот не админ / неверный chat_id / кривой Markdown.
                logger.error(
                    "Ошибка публикации [HTTP %s]: %s",
                    resp.status,
                    data.get("description", data),
                )
                return False

        except aiohttp.ClientError as exc:
            logger.error("Сетевая ошибка публикации: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover
            logger.exception("Неожиданная ошибка публикации: %s", exc)
            return False

    async def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        """Update one already published incident message."""
        if not message_id:
            return False
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:TG_TEXT_LIMIT],
                   "disable_web_page_preview": True}
        promo = self._promo_markup()
        if promo:
            payload["reply_markup"] = promo
        try:
            session = await self._get_session()
            url = self._api_url.rsplit("/", 1)[0] + "/editMessageText"
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    logger.info("Обновлена публикация %s/%s.", chat_id, message_id)
                    return True
                logger.warning("Не удалось обновить публикацию: %s", data.get("description", data))
                return False
        except aiohttp.ClientError as exc:
            logger.warning("Сетевая ошибка обновления публикации: %s", exc)
            return False
