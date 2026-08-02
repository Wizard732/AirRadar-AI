"""interests_handler.py — конвейер классификации постов (Interests-модуль).

Поток: новый пост из user-канала → очистка → дедупликация → LLM-классификация
тем → рассылка в ЛС всем подписчикам совпавших тем.

Отдельный от военного (_process_message) конвейер: военный гоняет посты через
fast_filter+summarize, этот — через classify(тем). Сходство: дедуп и БД общие.
"""

from __future__ import annotations

import logging

from ai_summarizer import SummarizerProtocol
from database import Database
from dedup import DedupCache
from fast_filter import clean_signature
from interests_config import TOPIC_CLASSIFY_PROMPT, TOPIC_SLUGS, topic_label

logger = logging.getLogger("airradar.interests")


async def process_interests_message(
    event,  # noqa: ANN001  (events.NewMessage.Event)
    summarizer: SummarizerProtocol,
    dedup: DedupCache,
    db: Database,
    bot_client=None,
) -> None:
    """Классифицировать пост user-канала и разослать подписчикам тем.

    Полностью изолирован от военного конвейера. Ошибки логируются, не роняют бот.
    bot_client: TelegramClient бота — для рассылки в ЛС (если None, только классификация).
    """
    message = event.message
    text = (message.text or message.message or "").strip()
    if not text or len(text) < 20:
        return  # слишком короткое / медиа без текста

    # 1) Очистка подписи канала.
    text = clean_signature(text)

    # 2) Дедупликация (общая с военным модулем — кросс-посты не дублируются).
    if dedup.is_duplicate(text):
        logger.debug("Interests: дубликат пропущен: %s", text[:60])
        return

    source = getattr(event.chat, "username", None) or getattr(event.chat, "id", "?")
    # Отмечаем канал живым (для /status админки).
    db.channel_seen(str(source), "interests")

    try:
        # 3) LLM-классификация тем.
        raw = await summarizer.classify(text, TOPIC_CLASSIFY_PROMPT)
        topics = _parse_topics(raw)
        if not topics:
            logger.debug("Interests: пост не отнесён к темам (%s): %s", source, text[:60])
            return

        # 4) Журнал классификации (для будущих дайджестов/аналитики).
        db.save_classification(source=str(source), text=text, topics=",".join(topics))
        logger.info("Interests: %s -> темы %s (%s)", source, topics, text[:60])

        # 5) Рассылка подписчикам совпавших тем.
        if bot_client is not None:
            await _notify_topic_subscribers(bot_client, db, topics, source, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Interests: сбой обработки (%s): %s", source, exc)
        db.channel_error(str(source), "interests", str(exc))


def _parse_topics(raw: str) -> list[str]:
    """Распарсить ответ LLM ('crypto,economy') в список валидных slug-ов."""
    if not raw:
        return []
    raw = raw.strip().lower()
    if raw == "none":
        return []
    valid = set(TOPIC_SLUGS)
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        slug = part.strip()
        if slug in valid and slug not in out:
            out.append(slug)
    return out


async def _notify_topic_subscribers(bot_client, db: Database, topics: list[str], source: str, text: str) -> None:
    """Разослать пост в ЛС всем подписчикам совпавших тем (best effort)."""
    notified: set[int] = set()
    sent = 0
    # Шапка с темами и источником.
    tags = " ".join(topic_label(t) for t in topics)
    header = f"📰 {tags}\n📍 {source}\n\n"
    payload = (header + text).strip()
    if len(payload) > 4000:
        payload = payload[:4000]

    for topic in topics:
        # Только instant-подписчики; digest-подписчики получают сводкой (digest.py).
        for user_id in db.topic_subscribers(topic, mode="instant"):
            if user_id in notified:
                continue
            notified.add(user_id)
            try:
                await bot_client.send_message(user_id, payload, link_preview=False)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Interests: не отправлено %s: %s", user_id, exc)
    if sent:
        logger.info("Interests: рассылка отправлена %d получателям (темы: %s)", sent, topics)
