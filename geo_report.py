"""geo_report.py — приём геопозиции от пользователей как репорт угрозы.

Человек в ЛС бота жмёт «Прикрепить» → «Локация» (или /report — подсказка).
Бот сохраняет точку в geo_reports (с округлением до ~100 м и антиспамом
1 репорт / 5 мин) и подтверждает. Точки появляются на живой карте отдельным
слоём «👤 Повідомлення» (health_server /api/threats → reports).

Репорты НЕ публикуются как алерты и НЕ влияют на рассылку — только карта.
Это сознательная консервативность: репорт легко заспамить, а паника в ЛС
хуже, чем недостающая точка.
"""

from __future__ import annotations

import logging
import time

from telethon import TelegramClient, events

from city_coords import nearest_place
from kyiv_time import fmt as kyiv_fmt
from database import Database

logger = logging.getLogger(__name__)

REPORT_HINT = (
    "📍 <b>Повідомити про загрозу</b>\n\n"
    "Надішли свою геопозицію: скріпка 📎 → «Локація» → «Надіслати мою "
    "поточну локацію» прямо в цей чат.\n\n"
    "Точка з'явиться на живій карті як повідомлення від користувача — це "
    "допомагає іншим бачити загрозу поруч.\n\n"
    "⚠️ Координати округлюються (~100 м), твої дані ніде не публікуються. "
    "Не більше 1 повідомлення на 5 хвилин.\n"
    "🚨 За реальної небезпеки спочатку 102/101/112 — карта не замінює ДСНС."
)


def register_geo_report_handlers(bot: TelegramClient, db: Database) -> None:
    """Навесить обработчики /report и геопозиции (только ЛС)."""

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/report$"))
    async def _report_hint(event) -> None:  # noqa: ANN001
        await event.respond(REPORT_HINT, parse_mode="html")

    @bot.on(
        events.NewMessage(
            incoming=True,
            func=lambda e: getattr(getattr(e, "message", None), "geo", None) is not None
            and e.is_private,
        )
    )
    async def _geo_received(event) -> None:  # noqa: ANN001
        geo = event.message.geo
        lat, lon = float(geo.lat), float(geo.long)
        text = (event.message.text or "").strip()
        where = nearest_place(lat, lon)
        ok = db.add_geo_report(event.sender_id, lat, lon, text=text, region="")
        if ok:
            when = kyiv_fmt()
            logger.info("Geo-репорт от %s: %s (%.3f, %.3f)", event.sender_id, where, lat, lon)
            await event.respond(
                f"✅ Дякуємо! Повідомлення зафіксовано: <b>{where}</b>, {when}.\n"
                "Точка з'явиться на живій карті — видно буде лише округлені "
                "координати.",
                parse_mode="html",
            )
        else:
            wait_min = db.seconds_until_geo_report_allowed(event.sender_id) // 60 + 1
            await event.respond(
                f"⏳ Ти вже надіслав повідомлення недавно. Наступне можна "
                f"приблизно за {wait_min} хв.",
                parse_mode="html",
            )
