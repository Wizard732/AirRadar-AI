"""webpush_push.py — Web Push для iPhone-PWA (iOS 16.4+, застосунок додано «На екран Додому»).

Без входящих портів: PWA показує код підключення, користувач надсилає його боту (/pairweb КОД), сервер шле пуші
НАЗОВНІ через pywebpush на адресу Apple/Google. Потрібні VAPID-ключі (gen_vapid.py); без них модуль мовчки вимкнений.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

logger = logging.getLogger(__name__)
MAX_DEVICES = 3


def vapid_private() -> str:
    return os.getenv("VAPID_PRIVATE_KEY_FILE", "vapid_private.pem")


def configured() -> bool:
    return os.path.exists(vapid_private())


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS web_push_subs (endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL, sub_json TEXT NOT NULL)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def decode_code(code: str) -> dict | None:
    """Код підключення = base64url(JSON підписки). Повертає підписку або None, якщо формат хибний."""
    try:
        pad = "=" * (-len(code) % 4)
        sub = json.loads(base64.urlsafe_b64decode(code + pad).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    keys = sub.get("keys") or {}
    if not str(sub.get("endpoint", "")).startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return None
    return sub


def pair(db, uid: int, code: str) -> tuple[bool, str]:
    sub = decode_code(code.strip())
    if sub is None:
        return False, "Код підключення некоректний. Скопіюйте його з сторінки «Web Push» ще раз."
    _ensure(db)
    with db._lock:  # noqa: SLF001
        n = db._conn.execute("SELECT COUNT(*) FROM web_push_subs WHERE user_id=? AND endpoint<>?", (uid, sub["endpoint"])).fetchone()[0]  # noqa: SLF001
        if n >= MAX_DEVICES:
            return False, f"Можна підключити до {MAX_DEVICES} пристроїв. Надішліть /unpairweb і спробуйте знову."
        db._conn.execute("INSERT OR REPLACE INTO web_push_subs(endpoint,user_id,sub_json) VALUES (?,?,?)", (sub["endpoint"], uid, json.dumps(sub)))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True, "✅ iPhone підключено: тривоги приходитимуть як Web Push."


def unpair_all(db, uid: int) -> int:
    _ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM web_push_subs WHERE user_id=?", (uid,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount


def subs_of(db, uid: int) -> list[dict]:
    _ensure(db)
    with db._lock:  # noqa: SLF001
        return [json.loads(r[0]) for r in db._conn.execute("SELECT sub_json FROM web_push_subs WHERE user_id=?", (uid,)).fetchall()]  # noqa: SLF001


def _send_sync(sub: dict, payload: dict) -> int:
    from pywebpush import WebPushException, webpush
    try:
        webpush(sub, json.dumps(payload, ensure_ascii=False), vapid_private_key=vapid_private(),
                vapid_claims={"sub": os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")}, ttl=300)
        return 201
    except WebPushException as exc:
        return getattr(getattr(exc, "response", None), "status_code", 0) or 0


async def push_user(db, uid: int, title: str, body: str) -> int:
    """Скільки пристроїв прийняли пуш. Протухлі підписки (404/410) видаляються."""
    if not configured():
        return 0
    ok = 0
    loop = asyncio.get_running_loop()
    for sub in subs_of(db, uid):
        status = await loop.run_in_executor(None, _send_sync, sub, {"title": title, "body": body[:400]})
        if status in (200, 201, 202):
            ok += 1
        elif status in (404, 410):
            with db._lock:  # noqa: SLF001
                db._conn.execute("DELETE FROM web_push_subs WHERE endpoint=?", (sub["endpoint"],))  # noqa: SLF001
                db._conn.commit()  # noqa: SLF001
    return ok


async def push_regions(db, regions: list[str], title: str, body: str) -> int:
    if not configured():
        return 0
    seen: set[int] = set()
    n = 0
    for slug in regions:
        for uid in db.get_subscribers(slug):
            if uid not in seen:
                seen.add(uid)
                try:
                    n += await push_user(db, uid, title, body)
                except Exception:  # noqa: BLE001
                    logger.warning("webpush: збій для %s", uid, exc_info=True)
    return n
