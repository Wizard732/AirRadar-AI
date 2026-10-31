"""publisher.py — публикация обработанных постов в целевой канал через Bot API.

Использует единый aiohttp-клиент (один ClientSession на всё время жизни приложения)
для запросов к ``https://api.telegram.org/bot<token>/sendMessage``. Ошибки
отправки (нет прав, превышена длина, rate-limit) логируются, но не роняют
конвейер: одно неудачное сообщение не должно стопить весь поток.

Строка «📡 Джерело: AirRadar AI» в конце поста делается кликабельной ссылкой
на наш канал (promo_url) — пассивная реклама вместо упоминания исходников.
Если ссылку построить нельзя или HTML-отправка не прошла — пост уходит plain.
"""

from __future__ import annotations

import asyncio
import html
import logging

import aiohttp

from safety_policy import check_publication

from alert_renderer import SOURCE_BRAND_LINE

logger = logging.getLogger(__name__)

# Жёсткий лимит Telegram на длину sendMessage. На практике ИИ-выжимка + заголовок
# укладываются далеко в предел, но подстрахуемся от аномально длинного оригинала
# при fallback'е.
TG_TEXT_LIMIT = 4096


def build_linked_text(text: str, promo_url: str) -> str | None:
    """HTML-версия поста, где бренд-строка источника — ссылка на наш канал.

    Первая строка (заголовок алерта, trusted — построен рендером) оборачивается
    в <b> для визуального якоря в ленте. Тело поста экранируется (источник
    недоверенный), поэтому ответ «can't parse entities» невозможен.
    Возвращает None, когда ссылку строить не нужно/нельзя — вызывающий шлёт
    plain-текст без parse_mode.
    """
    promo_url = (promo_url or "").strip()
    if not promo_url:
        return None
    body, sep, brand = text.rpartition("\n")
    if not sep or brand != SOURCE_BRAND_LINE:
        return None
    head, head_sep, rest = body.partition("\n\n")
    # Заголовок — только trusted-строка рендера (эмодзи + капс + класс);
    # если структура не та, шлём без жирного.
    head_html = f"<b>{html.escape(head)}</b>" if head_sep and head and "<" not in head else html.escape(body)
    if head_html != html.escape(body):
        rest_escaped = "\n\n" + html.escape(rest)
    else:
        rest_escaped = ""
    return (
        f"{head_html}{rest_escaped}\n"
        f'<a href="{html.escape(promo_url, quote=True)}">{html.escape(brand)}</a>'
    )


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
        self.promo_url = promo_url.strip()
        self.outbox_db = None          # Database для черги дошліх постів (виставляє main)
        self._last_transient = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _deliver(self, endpoint: str, payload: dict, attempts: int = 3) -> dict | bool:
        """POST к Bot API з повторами лише для тимчасових збоїв (мережа, 5xx, 429 із Retry-After).
        Result-объект при успехе, False при ошибке (з логом). Постійні помилки (400 тощо) не повторюються."""
        self._last_transient = False
        for attempt in range(attempts):
            delay = 1.0 * (2 ** attempt)
            try:
                session = await self._get_session()
                url = f"{self._api_url.rsplit('/', 1)[0]}/{endpoint}"
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("ok"):
                        return data.get("result") or {"chat": {"id": self._target}, "message_id": 0}
                    logger.error("Ошибка публикации [HTTP %s]: %s", resp.status, data.get("description", data))
                    if resp.status == 429:
                        delay = min(float((data.get("parameters") or {}).get("retry_after", delay)), 15.0)
                    elif resp.status < 500:
                        return False   # постійна помилка (400/403/…): повтор не допоможе
                    self._last_transient = True
            except aiohttp.ClientError as exc:
                logger.error("Сетевая ошибка публикации: %s", exc)
                self._last_transient = True
            except Exception as exc:  # pragma: no cover
                logger.exception("Неожиданная ошибка публикации: %s", exc)
                return False
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return False

    async def send(self, text: str) -> dict | None:
        """Отправить текст в целевой канал.

        Возвращает result-объект Telegram при успехе, None/False при ошибке
        (с логированием причины). Скрипт при этом продолжает работать —
        следующее сообщение уйдёт нормально.
        """
        if not text:
            logger.debug("Пустой текст — публикация пропущена.")
            return False

        # Обов'язковий детерміновано-безпековий етап (safety_policy) перед будь-якою публікацією.
        verdict = check_publication(text)
        if not verdict.allowed:
            logger.warning("safety_policy заблокувала публікацію: %s", ",".join(verdict.violations))
            return False
        text = verdict.text

        # Подстраховка от лимита Telegram: режем с конца, заголовок (в начале) важнее.
        if len(text) > TG_TEXT_LIMIT:
            text = text[:TG_TEXT_LIMIT]
            logger.warning("Сообщение обрезано до %d символов перед отправкой.", TG_TEXT_LIMIT)

        # Channel posts and LLM output are untrusted. Публикуем через HTML
        # (ссылка на наш канал в строке «Джерело»); тело полностью экранировано,
        # а при любом сбое повторяем plain-текстом без parse_mode.
        linked = build_linked_text(text, self.promo_url)
        if linked is not None and len(linked) <= TG_TEXT_LIMIT:
            sent = await self._deliver("sendMessage", {
                "chat_id": self._target,
                "text": linked,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if sent:
                logger.info("Опубликовано в %s (%d символов).", self._target, len(text))
                return sent
            logger.warning("HTML-публикация не прошла — повтор plain-текстом без ссылки.")

        payload = {
            "chat_id": self._target,
            "text": text,
            "disable_web_page_preview": True,
        }
        sent = await self._deliver("sendMessage", payload)
        if sent:
            logger.info("Опубликовано в %s (%d символов).", self._target, len(text))
        elif self._last_transient and self.outbox_db is not None:
            self._enqueue_outbox("sendMessage", payload)   # тимчасовий збій: дошлемо воркером (TTL 10 хв), без дублювання
        return sent or False

    def _enqueue_outbox(self, endpoint: str, payload: dict) -> None:
        import hashlib
        import json
        import time

        from airradar.delivery import notify_queue as nq
        try:
            key = hashlib.sha1(f"{endpoint}|{payload.get('text', '')}|{int(time.time() // 600)}".encode()).hexdigest()[:24]
            nq.enqueue(self.outbox_db, key, 0, "channel", kind="post", ttl_s=600,
                       payload=json.dumps({"endpoint": endpoint, "payload": payload}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            logger.warning("outbox: не вдалося поставити в чергу", exc_info=True)

    async def outbox_worker(self, interval_s: int = 15) -> None:
        """Дошле пости в канал, що не пішли через тимчасовий збій."""
        import json

        from airradar.delivery import notify_queue as nq
        while True:
            try:
                for job in nq.claim_due(self.outbox_db, 20, channel="channel"):
                    data = json.loads(job.get("payload") or "{}")
                    ok = await self._deliver(data.get("endpoint", "sendMessage"), data.get("payload", {}), attempts=1)
                    if ok:
                        nq.mark_sent(self.outbox_db, job["id"], str((ok or {}).get("message_id", "")) if isinstance(ok, dict) else "")
                    else:
                        nq.mark_failed(self.outbox_db, job["id"], "channel_send_failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("outbox_worker")
            await asyncio.sleep(interval_s)

    async def send_photo(self, png: bytes, caption: str) -> bool:
        """Фото (безпечне превʼю карти) у канал; підпис проходить safety_policy так само, як текст."""
        verdict = check_publication(caption, llm_generated=False)
        if not verdict.allowed:
            return False
        try:
            session = await self._get_session()
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self._target))
            form.add_field("caption", caption[:1000])
            form.add_field("photo", png, filename="airradar.png", content_type="image/png")
            url = f"{self._api_url.rsplit('/', 1)[0]}/sendPhoto"
            async with session.post(url, data=form) as resp:
                data = await resp.json()
                return resp.status == 200 and bool(data.get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Не вдалося опублікувати превʼю карти: %s", exc)
            return False

    async def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        """Update one already published incident message."""
        if not message_id:
            return False
        verdict = check_publication(text)
        if not verdict.allowed:
            logger.warning("safety_policy заблокувала оновлення: %s", ",".join(verdict.violations))
            return False
        text = verdict.text[:TG_TEXT_LIMIT]
        linked = build_linked_text(text, self.promo_url)
        if linked is not None and len(linked) <= TG_TEXT_LIMIT:
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": linked,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if await self._deliver("editMessageText", payload):
                logger.info("Обновлена публикация %s/%s.", chat_id, message_id)
                return True
            logger.warning("HTML-обновление не прошло — повтор plain-текстом без ссылки.")
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if await self._deliver("editMessageText", payload):
            logger.info("Обновлена публикация %s/%s.", chat_id, message_id)
            return True
        return False
