"""admin_notify.py — усі службові звіти йдуть ЛИШЕ адмінам (супер-адмін з ADMIN_ID + додані в БД)."""
from __future__ import annotations

import logging

from database import Database

logger = logging.getLogger(__name__)


def admin_recipients(db: Database, super_admin_id: int) -> list[int]:
    ids: list[int] = []
    for uid in [super_admin_id, *db.all_admins()]:
        if uid and uid not in ids:
            ids.append(int(uid))
    return ids


async def notify_admins(bot, db: Database, super_admin_id: int, text: str, **kw) -> int:
    """Надіслати текст усім адмінам; повертає кількість успішних доставок."""
    sent = 0
    for uid in admin_recipients(db, super_admin_id):
        try:
            await bot.send_message(uid, text, **kw)
            sent += 1
        except Exception:  # noqa: BLE001 — один недоступний адмін не блокує решту
            logger.warning("Не вдалося надіслати звіт адміну %s", uid, exc_info=True)
    return sent
