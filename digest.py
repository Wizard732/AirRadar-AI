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


async def digest_scheduler(bot_client, db: Database, morning_hour: int, evening_hour: int, summarizer=None, publisher=None) -> None:
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
            # Сброс отметок в полночь (UTC).
            if cur_hour == 0 and now.tm_min < 10:
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

            # Ежедневный прогноз в 19:05 UTC = 22:05 по киевскому времени.
            # Проверяем час и минуту (планировщик тикает каждые 5 мин).
            cur_min = now.tm_min
            if cur_hour == 19 and cur_min >= 5 and "evening" not in last_run:
                try:
                    n = await _send_evening_forecasts(bot_client, db, publisher)
                    last_run.add("evening")
                    if n:
                        logger.info("Сводка за день отправлена: %d получателям + канал", n)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Сбой вечернего прогноза: %s", exc)
    except asyncio.CancelledError:
        logger.info("Планировщик дайджестов остановлен")
        raise


async def _send_evening_forecasts(bot_client, db: Database, publisher=None) -> int:
    """Разослать вечерний прогноз всем подписчикам регионов + в канал.

    publisher: если передан — сводка по всем активным регионам отправляется
    и в целевой канал тоже (не только в ЛС подписчикам).
    """
    from forecast import daily_forecast
    from regions import region_name as _rname
    import time

    sent = 0

    # 1) ОПРЕДЕЛЯЕМ РЕГИОНЫ ДЛЯ СВОДКИ.
    # Сначала — подписанные; если их нет — топ-5 по активности за день.
    try:
        sub_rows = db._conn.execute(
            "SELECT DISTINCT region FROM subscriptions"
        ).fetchall()
        regions = [r["region"] for r in sub_rows]
    except Exception:
        regions = []

    if not regions:
        # Нет подписок — берём топ-5 регионов за последние 24 часа.
        day_ago = int(time.time()) - 86400
        try:
            top = db._conn.execute(
                "SELECT region, COUNT(*) c FROM threats "
                "WHERE region != 'unknown' AND ts >= ? "
                "GROUP BY region ORDER BY c DESC LIMIT 5",
                (day_ago,),
            ).fetchall()
            regions = [r["region"] for r in top]
        except Exception:
            regions = []

    # 2) ЛС подписчикам (только подписанные регионы).
    for region in regions:
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

    # 3) В КАНАЛ — общая сводка по всем активным регионам.
    if publisher is not None:
        channel_text = await _build_channel_summary(db, regions)
        if channel_text:
            try:
                await publisher.send(channel_text)
                logger.info("Сводка за день отправлена в канал")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось отправить сводку в канал: %s", exc)

    return sent


async def _build_channel_summary(db: Database, regions: list[str]) -> str:
    """Общая сводка за день для канала — кратко по всем активным регионам."""
    from forecast import daily_forecast
    import time

    now_str = time.strftime("%d.%m.%Y")
    lines = [f"🌙 Підсумок дня — {now_str}\n"]

    # Общая статистика за день (все регионы).
    day = int(time.time()) - 86400
    counts = db.threat_counts(region=None, since=day)
    total = sum(counts.values())
    if total == 0:
        return ""  # нечего слать
    lines.append(f"🚨 Всього за день: {total} загроз\n")

    # Топ-5 регионов по активности.
    try:
        top = db._conn.execute(
            "SELECT region, COUNT(*) c FROM threats "
            "WHERE region != 'unknown' AND ts >= ? "
            "GROUP BY region ORDER BY c DESC LIMIT 5",
            (day,),
        ).fetchall()
        if top:
            lines.append("📍 Топ регіонів за день:")
            from regions import region_name as _rname
            for r in top:
                lines.append(f"  {_rname(r['region'])}: {r['c']}")
            lines.append("")
    except Exception:
        pass

    # Прогноз на ночь по топ-3 регионам (кратко).
    if regions:
        lines.append("📊 Прогноз на ніч:")
        for region in regions[:3]:
            fc = daily_forecast(db, region)
            if fc:
                # Берём только строку прогноза (последний блок).
                prognosis = ""
                for line in fc.split("\n"):
                    if "Історично" in line or "Висока" in line or "Помірна" in line or "Спокій" in line:
                        prognosis = line.strip()
                        break
                if prognosis:
                    lines.append(f"  {_rname(region)}: {prognosis}")

    lines.append("\n🛡 AirRadar AI")
    return "\n".join(lines)[:3900]
