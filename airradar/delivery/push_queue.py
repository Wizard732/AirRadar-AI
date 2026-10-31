"""push_queue.py — доставка push (FCM/ntfy, Web Push) через персистентну чергу: ідемпотентність, retry, відновлення.

Канал реєструє відправника: `register("fcm", async fn(db, uid, payload) -> int)`, де int — скільки пристроїв прийняли пуш.
deliver(): запис у чергу (ключ = пост+текст+10-хв вікно) → негайна спроба → при 0 прийнятих — retrying (backoff, TTL 10 хв:
запізнілий пуш про тривогу гірший за відсутній). retry_worker(): фоновий цикл.
Користувачі без пристроїв у чергу не потрапляють (викликач перевіряє наявність пристроїв до deliver).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from airradar.delivery import notify_queue as nq

logger = logging.getLogger(__name__)
SENDERS: dict = {}
BUCKET_S = 600
TTL_S = 600


def register(channel: str, fn) -> None:
    SENDERS[channel] = fn


def _key(dkey: str, payload: dict) -> str:
    bucket = int(time.time() // BUCKET_S)
    return hashlib.sha1(f"{dkey}|{payload.get('title')}|{payload.get('body')}|{bucket}".encode()).hexdigest()[:24]


async def deliver(db, channel: str, dkey: str, uid: int, payload: dict, ttl_s: int = TTL_S) -> int:
    """Скільки пристроїв прийняли пуш зараз (0 — дубль або помилка; помилка піде в повтор)."""
    fn = SENDERS.get(channel)
    if fn is None:
        return 0
    key = _key(dkey, payload)
    nq.enqueue(db, key, uid, channel, kind="push", ttl_s=ttl_s, payload=json.dumps(payload, ensure_ascii=False))
    job = nq.claim_one(db, key, uid, channel)
    if job is None:
        return 0
    try:
        n = await fn(db, uid, payload)
    except Exception as exc:  # noqa: BLE001 — push не повинен ламати основний потік
        nq.mark_failed(db, job["id"], type(exc).__name__)
        return 0
    if n > 0:
        nq.mark_sent(db, job["id"], f"devices={n}")
        return n
    nq.mark_failed(db, job["id"], "no_device_accepted")
    return 0


async def retry_worker(db, interval_s: int = 15) -> None:
    while True:
        try:
            for ch, fn in list(SENDERS.items()):
                for job in nq.claim_due(db, 50, channel=ch):
                    try:
                        n = await fn(db, job["user_id"], json.loads(job.get("payload") or "{}"))
                        if n > 0:
                            nq.mark_sent(db, job["id"], f"devices={n}")
                        else:
                            nq.mark_failed(db, job["id"], "no_device_accepted")
                    except Exception as exc:  # noqa: BLE001
                        nq.mark_failed(db, job["id"], type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("push retry_worker")
        await asyncio.sleep(interval_s)
