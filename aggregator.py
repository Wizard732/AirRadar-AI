"""aggregator.py — окно агрегации сообщений одного инцидента перед публикацией.

Несколько источников часто сообщают об одном и том же событии с разницей в
десятки секунд. Вместо трёх одинаковых постов в канал сообщения одного
инцидента (регион назначения + класс оружия) склеиваются в окно
``AGGREGATE_WINDOW_SEC`` и уходят одним постом со списком источников.

Байпас (немедленный флаш, без ожидания окна):
  * класс оружия CRITICAL (баллистика, крылатые, КАБ, прилёт…) —
  * отбой (stand_down) — по правилу «отбой публикуется всегда и мгновенно»,
  * материальное обновление инцидента (новый независимый источник, дельта).

Таймер окна на ключ НЕ сбрасывается новыми сообщениями: окно считается от
первого сообщения инцидента. ``aclose()`` выливает все буферы при остановке.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from incident_fusion import IncidentFact
from weapon_classes import weapon_severity

logger = logging.getLogger(__name__)


@dataclass
class PendingAlert:
    """Одно обработанное сообщение, ожидающее публикации."""

    text: str
    source: str
    event_ts: int
    fact: IncidentFact
    confirmation: dict
    regions: list[str] = field(default_factory=list)


class AlertAggregator:
    """Буфер алертов по ключу (регион, оружие) с окном window_sec.

    flush_callback: async callable(list[PendingAlert]) — публикует набор
    одним постом (в main.py это _publish_items).
    """

    def __init__(self, window_sec: float, flush_callback) -> None:
        self._window = max(0.05, float(window_sec))
        self._flush = flush_callback
        self._buffers: dict[tuple[str, str], list[PendingAlert]] = {}
        self._timers: dict[tuple[str, str], asyncio.Task] = {}
        self._closed = False

    def _is_bypass(self, alert: PendingAlert) -> bool:
        """Немедленный флаш: критично / отбой / материальное обновление."""
        if alert.fact.weapon_class == "stand_down":
            return True
        if alert.confirmation.get("material_update"):
            return True
        return weapon_severity(alert.fact.weapon_class) == "CRITICAL"

    async def submit(self, alert: PendingAlert) -> None:
        """Добавить сообщение в буфер; при байпасе — сразу опубликовать."""
        if self._closed:
            # После закрытия не копим — публикуем напрямую, ничего не теряем.
            await self._flush([alert])
            return
        key = (alert.fact.destination_region, alert.fact.weapon_class)
        self._buffers.setdefault(key, []).append(alert)
        if self._is_bypass(alert):
            await self._flush_key(key)
            return
        # Таймер ставится только на первый сообщение ключа и НЕ сбрасывается.
        if key not in self._timers:
            self._timers[key] = asyncio.create_task(self._expire(key))

    async def _expire(self, key: tuple[str, str]) -> None:
        """Окно истекло — опубликовать накопленное по ключу."""
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return  # буфер уже вылит через _flush_key/aclose
        self._timers.pop(key, None)
        await self._flush_key(key)

    async def _flush_key(self, key: tuple[str, str]) -> None:
        """Вылить один буфер. Ошибки публикации не роняют агрегатор."""
        items = self._buffers.pop(key, None)
        timer = self._timers.pop(key, None)
        if timer is not None and not timer.done():
            timer.cancel()
        if not items:
            return
        try:
            await self._flush(items)
        except Exception:  # noqa: BLE001 — конвейер не должен падать
            logger.exception("Сбой публикации агрегированного поста")

    async def aclose(self) -> None:
        """Graceful shutdown: вылить все буферы и снять таймеры."""
        self._closed = True
        for key in list(self._buffers.keys()):
            await self._flush_key(key)
