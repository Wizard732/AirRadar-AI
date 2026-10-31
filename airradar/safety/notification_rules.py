"""notification_rules.py — важливість сповіщень, зміни події та життєвий цикл доставки.

Критичні офіційні сповіщення не можна вимкнути налаштуваннями фільтрації звичайних повідомлень.
Статуси доставки чесні: sent ≠ delivered ≠ opened.
"""
from __future__ import annotations

from enum import Enum


class Importance(str, Enum):
    CRITICAL = "critical"      # офіційна тривога / підтверджена важлива подія
    HIGH = "high"              # зміна офіційного статусу, суттєве оновлення
    INFO = "info"              # загальні зведення, зміни обстановки
    ANALYTIC = "analytic"      # прогнози, статистика, історія


ORDER = [Importance.CRITICAL, Importance.HIGH, Importance.INFO, Importance.ANALYTIC]


class Change(str, Enum):
    NEW_EVENT = "new_event"
    MATERIAL_UPDATE = "material_update"
    OFFICIAL_ALARM = "official_alarm"
    OFFICIAL_CLEAR = "official_clear"
    EVENT_ENDED = "event_ended"
    CLARIFIED = "clarified"


_CHANGE_IMPORTANCE = {
    Change.OFFICIAL_ALARM: Importance.CRITICAL,
    Change.OFFICIAL_CLEAR: Importance.HIGH,
    Change.MATERIAL_UPDATE: Importance.HIGH,
    Change.NEW_EVENT: Importance.INFO,
    Change.CLARIFIED: Importance.INFO,
    Change.EVENT_ENDED: Importance.INFO,
}


def importance_for(change: Change, *, confirmed: bool = False) -> Importance:
    """Важливість за типом зміни. Підтверджена нова подія піднімається до CRITICAL."""
    if change is Change.NEW_EVENT and confirmed:
        return Importance.CRITICAL
    return _CHANGE_IMPORTANCE[change]


def user_wants(importance: Importance, enabled: set[Importance] | None) -> bool:
    """Чи доставляти користувачу. CRITICAL доставляється ЗАВЖДИ, незалежно від фільтрів."""
    if importance is Importance.CRITICAL:
        return True
    return enabled is None or importance in enabled


def is_repeat(prev: tuple[Change, str] | None, change: Change, fingerprint: str) -> bool:
    """Не повторювати однакову інформацію: та сама зміна з тим самим відбитком — дубль."""
    return prev is not None and prev == (change, fingerprint)


# --- Життєвий цикл доставки -----------------------------------------------------
class Delivery(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"        # лише якщо канал реально дає підтвердження
    OPENED = "opened"              # лише якщо застосунок це вміє
    ACKNOWLEDGED = "acknowledged"  # користувач натиснув «Отримав»
    FAILED = "failed"


_RANK = {Delivery.CREATED: 0, Delivery.QUEUED: 1, Delivery.SENT: 2, Delivery.DELIVERED: 3,
         Delivery.OPENED: 4, Delivery.ACKNOWLEDGED: 5}


def can_transition(cur: Delivery, new: Delivery) -> bool:
    """Статус рухається лише вперед; FAILED — з будь-якого, крім підтверджених користувачем."""
    if cur is Delivery.FAILED:
        return new in (Delivery.QUEUED, Delivery.SENT)   # повторна спроба
    if new is Delivery.FAILED:
        return _RANK[cur] < _RANK[Delivery.DELIVERED]
    return _RANK[new] > _RANK[cur]
