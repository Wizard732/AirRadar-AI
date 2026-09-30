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

    def __init__(self, window_sec: float, flush_callback,
                 critical_needs_confirmation: bool = False) -> None:
        self._window = max(0.05, float(window_sec))
        self._flush = flush_callback
        self._critical_gate = critical_needs_confirmation
        self._buffers: dict[tuple[str, str], list[PendingAlert]] = {}
        self._timers: dict[tuple[str, str], asyncio.Task] = {}
        self._closed = False

    def _is_bypass(self, alert: PendingAlert) -> bool:
        """Немедленный флаш: подтверждено / отбой / материальное обновление.

        Гард подтверждения (critical_needs_confirmation=True): CRITICAL-класс
        (балістика, крилаті, КАБ…) от одного источника НЕ байпасит и не
        публикуется вовсе, пока инцидент не подтвердит второй независимый
        источник или официальный канал. Пост при этом записан в БД и виден
        на карте. Защита от ложных «загроза балістики» от одного канала.
        """
        if alert.fact.weapon_class == "stand_down":
            return True
        if alert.confirmation.get("material_update"):
            return True
        if not self._critical_gate:
            return weapon_severity(alert.fact.weapon_class) == "CRITICAL"
        if weapon_severity(alert.fact.weapon_class) != "CRITICAL":
            return False  # некритичный класс — обычное окно агрегации, без байпаса
        status = alert.confirmation.get("status", "reported")
        return status in ("corroborated", "officially_confirmed")

    def _confirm_ok(self, items: list[PendingAlert]) -> bool:
        """В наборе есть подтверждённый инцидент (2+ источника / официально)."""
        return any(
            it.confirmation.get("status") in ("corroborated", "officially_confirmed")
            for it in items
        )

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
        """Вылить один буфер. Ошибки публикации не роняют агрегатор.

        Гард подтверждения: если в буфере только одиночные непідтверджені
        CRITICAL-посты — окно истекает в тишину (посты уже в БД/карте).
        Как только в наборе есть подтверждённый инцидент — публикуется всё
        накопленное по ключу одним постом. Некритичные классы (БПЛА, отбой,
        артобстрел) гард не трогает: ключ = (регион, класс), класс у набора
        общий, поэтому проверки достаточно на ключе.
        """
        items = self._buffers.pop(key, None)
        timer = self._timers.pop(key, None)
        if timer is not None and not timer.done():
            timer.cancel()
        if not items:
            return
        if (
            self._critical_gate
            and weapon_severity(key[1]) == "CRITICAL"
            and not self._confirm_ok(items)
        ):
            logger.info(
                "CRITICAL без подтверждения не публикуется (ключ %s) — ждём 2-й источник",
                key,
            )
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
