"""alert_loop.py — фоновий цикл: обчислює алерти (з cooldown/відновленням) і шле їх лише адмінам."""
from __future__ import annotations

import asyncio
import logging

from airradar.delivery.admin_notify import notify_admins
from airradar.monitoring import alerts

logger = logging.getLogger(__name__)
INTERVAL_S = 300
_tick = [0]


async def alert_loop(db, bot, super_admin_id: int, interval_s: int = INTERVAL_S) -> None:
    await asyncio.sleep(60)  # дати системі прогрітись після старту
    while True:
        try:
            for msg in alerts.evaluate(db):
                await notify_admins(bot, db, super_admin_id, msg)
            if _tick[0] % 288 == 0:  # приблизно раз на добу
                from airradar.ops import retention
                retention.run(db)
            _tick[0] += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("alert_loop: збій обчислення алертів")
        await asyncio.sleep(interval_s)
