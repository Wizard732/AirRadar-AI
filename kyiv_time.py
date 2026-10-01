"""kyiv_time.py — единая точка киевского времени для всех строк постов.

Сервер (CT100) живёт в UTC, а пользователь — в Украине. До этого фикса
каждый модуль форматировал время через time.localtime()/strftime — в
контейнере это UTC, и в постах появлялось время на 2–3 часа раньше
киевского. Все пользовательские строки «🕒 HH:MM», часовые статистики
(«хвилі зазвичай ~X:00») и группировки по дням считаются здесь.

Переходы DST считаются вручную (последнее воскресенье марта/октября,
момент 01:00 UTC в обоих случаях) — как исторически в проекте
(main._kyiv_datetime / wave_forecast._kyiv_hour_of): на Windows-машинах
разработки нет пакета tzdata, а в slim-контейнерах zoneinfo может
отсутствовать. Теперь логика одна — модули делегируют сюда.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from functools import lru_cache

__all__ = [
    "kyiv_datetime",
    "kyiv_hour",
    "kyiv_date",
    "fmt",
    "kyiv_midnight_ts",
]


@lru_cache(maxsize=None)
def _dst_transition(year: int, month: int) -> datetime:
    """Последнее воскресенье месяца в 01:00 UTC (включение/выключение DST)."""
    day = datetime(year, month, 31, 1, 0, tzinfo=tz.utc)
    while day.weekday() != 6:  # воскресенье
        day -= timedelta(days=1)
    return day


def _utc_offset_hours(dt_utc: datetime) -> int:
    """Смещение Киева от UTC на момент dt_utc: 3 (EEST, лето) или 2 (EET)."""
    eest = _dst_transition(dt_utc.year, 3) <= dt_utc < _dst_transition(dt_utc.year, 10)
    return 3 if eest else 2


def kyiv_datetime(ts: float | int | None = None) -> datetime:
    """Текущее (или для unix-ts) время в Киеве как naive datetime."""
    if ts is None:
        dt = datetime.now(tz.utc)
    else:
        dt = datetime.fromtimestamp(int(ts), tz=tz.utc)
    return (dt + timedelta(hours=_utc_offset_hours(dt))).replace(tzinfo=None)


def kyiv_hour(ts: float | int | None = None) -> int:
    """Час суток в Киеве (0–23)."""
    return kyiv_datetime(ts).hour


def kyiv_date(ts: float | int | None = None, pattern: str = "%Y-%m-%d") -> str:
    """Дата в Киеве строкой (дефолт «РРРР-ММ-ДД»)."""
    return kyiv_datetime(ts).strftime(pattern)


def fmt(ts: float | int | None = None, pattern: str = "%H:%M") -> str:
    """Готовая строка времени в Киеве: fmt(ts), fmt(ts, "%d.%m %H:%M")."""
    return kyiv_datetime(ts).strftime(pattern)


def kyiv_midnight_ts(now: float | int | None = None) -> int:
    """Epoch-полночь сегодняшнего дня в Киеве (группировка статистики по дням)."""
    midnight = kyiv_datetime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    dt_utc = midnight.replace(tzinfo=tz.utc)
    return int(dt_utc.timestamp()) - _utc_offset_hours(dt_utc) * 3600
