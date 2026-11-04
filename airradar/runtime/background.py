"""Фоновые вспомогательные корутины бота (вынесено из main.py без изменения логики)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ai_summarizer import SummarizerProtocol
from database import Database

logger = logging.getLogger("airradar")


async def _report_session_failure(settings) -> None:
    """Один раз в час шлёт админу через Bot API: сессия слетела. Файл-маркер переживает рестарты."""
    import json as _json
    marker = Path(settings.db_path).with_name(".session_alert")
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
            return
        import aiohttp
        from admin_notify import admin_recipients
        recipients = admin_recipients(Database(settings.db_path), settings.admin_id)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            for chat in recipients:
                await http.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
                    json={"chat_id": chat,
                          "text": "🔴 AirRadar: сессия Telegram не авторизована, бот остановлен. "
                                  "Нужен повторный вход (код из Telegram)."},
                )
        marker.write_text(_json.dumps({"ts": int(time.time())}))
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отправить уведомление о слетевшей сессии", exc_info=True)


async def _healthcheck_loop(summarizer: SummarizerProtocol, interval: int) -> None:
    """Периодический пинг Ollama — раннее обнаружение падения локального ИИ."""
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await summarizer.healthcheck()
            if ok:
                logger.debug("Healthcheck Ollama: OK")
            else:
                logger.warning("Healthcheck Ollama: НЕДОСТУПНА — работает fallback на оригинал.")
    except asyncio.CancelledError:
        # Нормальный выход при остановке приложения.
        raise


async def _publish_notice(publisher, text: str) -> None:
    """Опубликовать прогнозное уведомление в канал (best effort).

    Прогнозы (countdown отбоя, «можлива нова тривога») раньше уходили
    только в ЛС подписчикам. По правилу продукта они должны быть видны
    и в канале. Бренд-строка в конце — как у обычных постов (Publisher
    делает её кликабельной ссылкой на наш канал).
    """
    if publisher is None:
        return
    from alert_renderer import SOURCE_BRAND_LINE

    try:
        sent = await publisher.send(f"{text}\n\n{SOURCE_BRAND_LINE}")
        if not sent:
            logger.warning("Прогноз не опубликован в канал: отправка не удалась")
    except Exception as exc:  # noqa: BLE001 — прогноз не должен ронять цикл
        logger.warning("Прогноз не опубликован в канал: %s", exc)


async def expire_alerts_loop(db: Database, interval: int = 600, max_age_seconds: int = 6 * 3600) -> None:
    """Закривати локальні статуси тривог без відбою старші max_age_seconds (з явним записом у лог).

    Це НЕ офіційний відбій: лише страховка від «вічної» тривоги в БД, якщо відбій не надійшов.
    Раніше закриття відбувалось неявно при відкритті екрана статистики.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            n = db.expire_stale_alerts(max_age_seconds)
            if n:
                logger.warning("Закрито за таймаутом (не відбій): %d статусів тривоги старше %d год", n, max_age_seconds // 3600)
        except Exception:  # noqa: BLE001 — фонова задача не падає
            logger.exception("expire_alerts_loop")


async def inbox_retry_loop(db: Database, process, interval: int = 30) -> None:
    """Повторна обробка постів, що не пройшли конвеєр через збій (R2).

    process: async callable(text, source, event_ts, inbox_id). Не більше MAX_ATTEMPTS спроб протягом MAX_AGE_S.
    """
    from airradar.processing.inbox import Inbox
    while True:
        await asyncio.sleep(interval)
        try:
            for row in Inbox.of(db).claim_retry():
                logger.warning("inbox: повтор обробки поста %s (спроба %d)", row["id"], row["attempts"] + 1)
                await process(row["text"], row["source"], row["event_ts"], row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — фонова задача не падає
            logger.exception("inbox_retry_loop")
