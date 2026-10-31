"""tg_delivery.py — доставка Telegram-сповіщень через персистентну чергу.

deliver(): ідемпотентний запис у чергу → негайна спроба → sent або retrying (backoff, FloodWait).
retry_worker(): фоновий цикл дошле те, що не вдалося (або застрягло після перезапуску).
Старий журнал `notification_delivery` (record_delivery) викликач веде як і раніше.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from airradar.delivery import notify_queue as nq

logger = logging.getLogger(__name__)
CHANNEL = "tg"


DEDUP_BUCKET_S = 600


def event_key_for(dkey: str, text: str, now: float | None = None) -> str:
    """Той самий пост у межах 10 хв = той самий ключ (ідемпотентність); оновлений текст або пізніший повтор
    (наприклад, нова тривога з тим самим текстом наступної ночі) — новий ключ, тож не відсікаються."""
    import time
    bucket = int((time.time() if now is None else now) // DEDUP_BUCKET_S)
    return hashlib.sha1(f"{dkey}|{text}|{bucket}".encode()).hexdigest()[:24]


async def _send(bot, user_id: int, payload: dict) -> str:
    kw = {"link_preview": False}
    if payload.get("feedback_key"):
        from airradar.app_ref import main_module
        kw["buttons"] = main_module()._feedback_kb(payload["feedback_key"], payload.get("src_text", payload["text"]))
    elif payload.get("ack_key"):
        from telethon import Button
        kw["buttons"] = [[Button.inline("✅ Отримав", data=f"ack:{payload['ack_key']}")]]
    msg = await bot.send_message(user_id, payload["text"], **kw)
    return str(getattr(msg, "id", "") or "")


async def deliver(bot, db, dkey: str, user_id: int, text: str, *, feedback_key: str = "", ack_key: str = "",
                  src_text: str = "", ttl_s: int = nq.DEFAULT_TTL_S) -> bool:
    """True — відправлено зараз. False — дубль, або помилка (буде повтор воркером)."""
    key = event_key_for(dkey, text)
    payload = {"text": text, "feedback_key": feedback_key, "ack_key": ack_key, "src_text": src_text}
    nq.enqueue(db, key, user_id, CHANNEL, ttl_s=ttl_s, payload=json.dumps(payload, ensure_ascii=False))
    job = nq.claim_one(db, key, user_id, CHANNEL)
    if job is None:
        return False   # уже відправлено або в роботі: ідемпотентність
    try:
        ext = await _send(bot, user_id, payload)
        nq.mark_sent(db, job["id"], ext)
        return True
    except Exception as exc:  # noqa: BLE001 — один недоступний користувач не зупиняє решту
        status = nq.mark_failed(db, job["id"], type(exc).__name__, retry_after_s=nq.flood_wait_seconds(exc))
        logger.debug("tg deliver %s: %s → %s", user_id, type(exc).__name__, status)
        return False


async def retry_worker(bot, db, interval_s: int = 10) -> None:
    while True:
        try:
            for job in nq.claim_due(db, 50, channel=CHANNEL):
                try:
                    payload = json.loads(job.get("payload") or "{}")
                    if not payload.get("text"):
                        nq.mark_failed(db, job["id"], "no_payload", retry_after_s=nq.MAX_BACKOFF_S * 100)
                        continue
                    nq.mark_sent(db, job["id"], await _send(bot, job["user_id"], payload))
                except Exception as exc:  # noqa: BLE001
                    nq.mark_failed(db, job["id"], type(exc).__name__, retry_after_s=nq.flood_wait_seconds(exc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("retry_worker")
        await asyncio.sleep(interval_s)
