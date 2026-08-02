"""digest.py — дайджесты по расписанию (ТЗ раздел 5.3).

Утром/вечером собирает посты по темам за прошедший период и рассылает
digest-подписчикам в ЛС. Instant-подписчики получают посты сразу (это делает
interests_handler), digest-подписчики — сводкой здесь.

Планировщик: каждые 5 минут проверяет, не наступил ли час дайджеста. Окно
сбора — 12 часов назад (утренний дайджест с вечера, вечерний — с утра).
"""

from __future__ import annotations

import asyncio
import logging
import time

from database import Database
from interests_config import topic_label

logger = logging.getLogger(__name__)

# Окно дайджеста (сколько часов назад собирать). Утро+вечер = 12ч каждый.
DIGEST_WINDOW = 12 * 3600
# Максимум постов на тему в одном дайджесте (чтобы не флудить).
MAX_POSTS_PER_TOPIC = 5


async def send_digests(bot_client, db: Database, period_name: str, summarizer=None) -> int:
    """Собрать и разослать дайджесты всем digest-подписчикам.

    period_name: 'Утренний' или 'Вечерний' (для заголовка).
    summarizer: если передан и постов 3+ — делает LLM-сводку («главное за день»,
    ТЗ 5.2). Если None/мало постов — просто список (старое поведение).
    Возвращает число отправленных дайджестов.
    """
    topics = db.all_digest_topics()
    if not topics:
        return 0  # нет digest-подписчиков — делать нечего

    since = int(time.time()) - DIGEST_WINDOW
    sent_total = 0
    for topic in topics:
        posts = db.recent_classified_posts(topic, since, limit=MAX_POSTS_PER_TOPIC)
        if not posts:
            continue
        # LLM-сводка (кластеризация), если постов достаточно и есть summarizer.
        summary = ""
        if summarizer is not None and len(posts) >= 3:
            from ai_summarizer import summarize_digest
            texts = [p["text"] for p in posts]
            summary = await summarize_digest(summarizer, topic_label(topic), texts)
        text = _format_digest(topic_label(topic), period_name, posts, summary)
        subscribers = db.digest_subscribers_by_topic(topic)
        sent = 0
        for user_id in subscribers:
            try:
                await bot_client.send_message(user_id, text, link_preview=False)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Дайджест: не отправлено %s: %s", user_id, exc)
        sent_total += sent
        logger.info("Дайджест %s [%s]: отправлено %d (постов: %d, LLM: %s)",
                    period_name, topic, sent, len(posts), bool(summary))
    return sent_total


def _format_digest(label: str, period_name: str, posts: list[dict], summary: str = "") -> str:
    """Собрать текст дайджеста.

    summary: если передана (LLM-сводка) — идёт вверху как «главное», а посты
    ниже как подробности. Если нет — просто список постов.
    """
    lines = [f"📰 {period_name} дайджест: {label}\n"]
    if summary:
        lines.append(summary.strip() + "\n")
        lines.append("—" * 20)
        lines.append("Подробности:")
    for p in posts:
        ago = int((time.time() - p["ts"]) // 3600)
        ago_str = f"{ago}ч" if ago > 0 else "только что"
        lines.append(f"• [{ago_str}] {p['text'][:200]}")
    lines.append(f"\nВсего: {len(posts)} пост(ов) за {DIGEST_WINDOW // 3600}ч")
    return "\n".join(lines)[:3900]


async def digest_scheduler(bot_client, db: Database, morning_hour: int, evening_hour: int, summarizer=None) -> None:
    """Бесконечный цикл: каждые 5 минут проверяет, не час ли дайджеста.

    Запускается как asyncio-таска из main.run(). Отменяется при остановке бота.
    summarizer: для LLM-сводки дайджеста (ТЗ 5.2).
    """
    last_run: set[int] = set()  # часы (UTC), уже отправленные сегодня
    logger.info("Планировщик дайджестов запущен (утро=%d:00, вечер=%d:00 UTC)",
                morning_hour, evening_hour)
    try:
        while True:
            await asyncio.sleep(300)  # проверка раз в 5 минут
            now = time.gmtime()
            cur_hour = now.tm_hour
            # Сброс отметок в полночь.
            if cur_hour == 0:
                last_run = set()
            # Если текущий час — час дайджеста и ещё не слали сегодня.
            if cur_hour in (morning_hour, evening_hour) and cur_hour not in last_run:
                period = "Утренний" if cur_hour == morning_hour else "Вечерний"
                try:
                    n = await send_digests(bot_client, db, period, summarizer)
                    last_run.add(cur_hour)
                    if n:
                        logger.info("Дайджест «%s» отправлен %d получателям", period, n)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Сбой дайджеста «%s»: %s", period, exc)

            # Ежедневный прогноз на ночь в 21:00 UTC (отдельный час от дайджестов).
            if cur_hour == 21 and 21 not in last_run:
                try:
                    n = await _send_evening_forecasts(bot_client, db)
                    last_run.add(21)
                    if n:
                        logger.info("Вечерний прогноз отправлен %d получателям", n)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Сбой вечернего прогноза: %s", exc)
    except asyncio.CancelledError:
        logger.info("Планировщик дайджестов остановлен")
        raise


async def _send_evening_forecasts(bot_client, db: Database) -> int:
    """Разослать вечерний прогноз всем подписчикам регионов (Military)."""
    from forecast import daily_forecast
    from database import Database as DB
    # Берём все регионы, на которые кто-то подписан (subscriptions).
    try:
        regions = db._conn.execute(
            "SELECT DISTINCT region FROM subscriptions"
        ).fetchall()
    except Exception:
        return 0
    sent = 0
    for row in regions:
        region = row["region"]
        text = daily_forecast(db, region)
        if not text:
            continue
        subscribers = db.get_subscribers(region)
        for user_id in subscribers:
            try:
                await bot_client.send_message(user_id, text, link_preview=False)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Прогноз: не отправлено %s: %s", user_id, exc)
    return sent
