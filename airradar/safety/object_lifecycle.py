"""object_lifecycle.py — життєвий цикл окремого повітряного об'єкта на карті (чистий модуль, без БД і мережі).

Доповнює event_lifecycle (стадії самої події: Created → … → Archived), а не замінює її:
тут — статус конкретної цілі (шахед, ракета) з історією змін.

Головні правила (ТЗ п. 4):
- відсутність інформації ≠ знищення: без оновлень ціль іде в stale → lost, але не в destroyed;
- destroyed / intercepted / fell ставляться лише з підтвердженням (хоча б одне підтверджуюче джерело);
- статуси, що закривають ціль (intercepted, destroyed, fell, completed), прибирають її з активного шару;
- expired — кінцевий: історія зберігається, на карті об'єкта немає.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    DETECTED = "detected"
    CONFIRMED = "confirmed"
    TRACKING = "tracking"
    STALE = "stale"
    LOST = "lost"
    INTERCEPTED = "intercepted"
    DESTROYED = "destroyed"
    FELL = "fell"
    COMPLETED = "completed"
    EXPIRED = "expired"


# Статуси, які потребують підтвердження джерелом (не можна ставити за замовчуванням).
CONFIRM_REQUIRED = {Status.INTERCEPTED, Status.DESTROYED, Status.FELL}
# Статуси, що закривають ціль: зупиняємо анімацію і прибираємо з активного шару.
CLOSED = {Status.INTERCEPTED, Status.DESTROYED, Status.FELL, Status.COMPLETED, Status.EXPIRED}
# Активний шар: ціль, яку ще можна показувати як поточну (з позначкою давності, якщо stale).
ACTIVE = {Status.DETECTED, Status.CONFIRMED, Status.TRACKING, Status.STALE}
# Анімація руху тільки для свіжих даних.
ANIMATED = {Status.TRACKING, Status.CONFIRMED}

_T = Status
TRANSITIONS: dict[Status, set[Status]] = {
    _T.DETECTED: {_T.CONFIRMED, _T.TRACKING, _T.STALE, _T.LOST, _T.INTERCEPTED, _T.DESTROYED, _T.FELL, _T.COMPLETED},
    _T.CONFIRMED: {_T.TRACKING, _T.STALE, _T.LOST, _T.INTERCEPTED, _T.DESTROYED, _T.FELL, _T.COMPLETED},
    _T.TRACKING: {_T.STALE, _T.LOST, _T.INTERCEPTED, _T.DESTROYED, _T.FELL, _T.COMPLETED},
    _T.STALE: {_T.TRACKING, _T.LOST, _T.INTERCEPTED, _T.DESTROYED, _T.FELL, _T.COMPLETED},
    _T.LOST: {_T.TRACKING, _T.COMPLETED, _T.EXPIRED},
    _T.INTERCEPTED: {_T.EXPIRED},
    _T.DESTROYED: {_T.EXPIRED},
    _T.FELL: {_T.EXPIRED},
    _T.COMPLETED: {_T.EXPIRED},
    _T.EXPIRED: set(),
}

# Тайминги (секунди). Значення — за замовчуванням; можна передати свої в evaluate_silence.
STALE_AFTER_S = 5 * 60          # немає оновлень 5 хв → stale (анімація зупиняється)
LOST_AFTER_S = 25 * 60          # немає оновлень 25 хв → lost (ціль ще видно з попередженням)
LOST_HIDE_AFTER_S = 10 * 60     # lost ще 10 хв на шарі, далі — з активного шару
EXPIRE_AFTER_S = 6 * 3600       # закрита ціль старша 6 год → expired

# Нормалізований тип → колір. Жовтий — беспілотна загроза, червоний — ракетна; решта — власні позначення.
KIND_CLASS = {
    "shahed": "drone", "uav": "drone", "recon_drone": "drone", "fpv": "drone",
    "cruise_missile": "missile", "ballistic": "missile", "missile": "missile",
    "air_missile": "missile", "coastal_missile": "missile",
    "kab": "bomb", "aviation": "aircraft", "tac_aviation": "aircraft", "strat_aviation": "aircraft",
    "mlrs": "artillery", "artillery": "artillery",
}
CLASS_COLOR = {"drone": "#ffd60a", "missile": "#ff3b30", "bomb": "#ffb020",
               "aircraft": "#5ac8fa", "artillery": "#ff9f0a", "other": "#9aa3b5"}


def class_of(kind: str) -> str:
    """Клас загрози з нормалізованого типу. Невідомий тип → «other», ніколи не дрон/ракета за замовчуванням."""
    return KIND_CLASS.get(kind or "", "other")


def color_for(kind: str) -> str:
    return CLASS_COLOR[class_of(kind)]


class TransitionError(ValueError):
    pass


@dataclass
class Change:
    ts: int
    frm: str | None
    to: str
    reason: str


@dataclass
class ObjectRecord:
    id: str
    kind: str
    detected_ts: int
    last_seen_ts: int
    status: Status = Status.DETECTED
    sources: set[str] = field(default_factory=set)      # усі джерела, що згадували об'єкт
    confirmed_by: set[str] = field(default_factory=set)  # джерела, що підтвердили закриття (для destroyed тощо)
    closed_ts: int | None = None
    history: list[Change] = field(default_factory=list)

    @property
    def trust(self) -> str:
        """Рівень довіри з кількості незалежних джерел: 1 — низький, 2 — середній, 3+ — високий."""
        n = len(self.sources)
        return "high" if n >= 3 else ("medium" if n == 2 else "low")

    @property
    def animated(self) -> bool:
        return self.status in ANIMATED

    @property
    def in_active_layer(self) -> bool:
        return self.status in ACTIVE


def _move(obj: ObjectRecord, new: Status, ts: int, reason: str) -> None:
    if new not in TRANSITIONS[obj.status]:
        raise TransitionError(f"{obj.id}: {obj.status.value} → {new.value} недозволено")
    obj.history.append(Change(ts, obj.status.value, new.value, reason))
    obj.status = new
    if new in CLOSED and obj.closed_ts is None:
        obj.closed_ts = ts


def create(obj_id: str, kind: str, ts: int, source: str) -> ObjectRecord:
    obj = ObjectRecord(id=obj_id, kind=kind, detected_ts=ts, last_seen_ts=ts, sources={source})
    obj.history.append(Change(ts, None, Status.DETECTED.value, "виявлено"))
    return obj


def observe(obj: ObjectRecord, ts: int, source: str, confirmed: bool = False) -> None:
    """Нове повідомлення про об'єкт: оновлює час і джерела; повертає stale/lost у tracking."""
    if obj.status in CLOSED:
        return   # закриту ціль не «оживляємо» — повторні згадки не змінюють історію
    obj.last_seen_ts = max(obj.last_seen_ts, ts)
    obj.sources.add(source)
    if obj.status is Status.DETECTED and confirmed:
        _move(obj, Status.CONFIRMED, ts, f"підтверджено: {source}")
    elif obj.status in (Status.DETECTED, Status.CONFIRMED, Status.STALE, Status.LOST):
        target = Status.TRACKING if obj.status is not Status.DETECTED or len(obj.sources) > 1 else Status.DETECTED
        if target is not obj.status:
            _move(obj, target, ts, "дані відновились" if obj.status in (Status.STALE, Status.LOST) else "нове спостереження")


def close(obj: ObjectRecord, new: Status, ts: int, source: str, reason: str = "") -> None:
    """Перехід у перехоплено / знищено / впало / завершено. Для підтверджуваних статусів потрібне джерело."""
    if new in CONFIRM_REQUIRED and not source:
        raise TransitionError(f"{obj.id}: {new.value} без підтвердження джерелом заборонено")
    if new in CONFIRM_REQUIRED:
        obj.confirmed_by.add(source)
    _move(obj, new, ts, reason or f"{new.value} ({source or 'внутрішнє'})")


def evaluate_silence(obj: ObjectRecord, now: int, stale_after: int = STALE_AFTER_S,
                     lost_after: int = LOST_AFTER_S, expire_after: int = EXPIRE_AFTER_S) -> Status:
    """Оновлює статус за давністю даних. Ніколи не ставить destroyed: відсутність інформації — не знищення."""
    silence = now - obj.last_seen_ts
    if obj.status in CLOSED:
        if obj.status is not Status.EXPIRED and obj.closed_ts is not None and now - obj.closed_ts >= expire_after:
            _move(obj, Status.EXPIRED, now, "строк зберігання минув")
        return obj.status
    if obj.status in (Status.TRACKING, Status.CONFIRMED, Status.DETECTED) and silence >= stale_after:
        _move(obj, Status.STALE, now, f"немає оновлень {silence // 60} хв")
    if obj.status in (Status.STALE, Status.DETECTED) and silence >= lost_after:
        _move(obj, Status.LOST, now, f"немає оновлень {silence // 60} хв")
    return obj.status


def visible(obj: ObjectRecord, now: int, lost_hide_after: int = LOST_HIDE_AFTER_S) -> bool:
    """Чи показувати на активному шарі: lost ще лишається певний час з попередженням, далі зникає."""
    if obj.status is Status.LOST:
        return now - obj.last_seen_ts < LOST_AFTER_S + lost_hide_after
    return obj.in_active_layer
