"""eta.py — оценка времени прилёта угрозы из истории.

Подход (по согласованию с пользователем): НЕ гадать физически, а считать
**среднее время между пуском и прилётом** по прошлым случаям в регионе.

Алгоритм:
  1. Берём все угрозы типа «в полёте» (missile/uav) в регионе за последние N дней.
  2. Берём все «прилёты» (explosion) в том же регионе.
  3. Для каждой угрозы-пуска ищем ближайший прилёт в течение 2 часов после.
  4. Считаем среднее время между ними — это и есть исторический ETA.

Пока пар мало (< MIN_PAIRS) — честно возвращаем, что данных недостаточно.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from database import Database

logger = logging.getLogger(__name__)

# Окно поиска пар пуск→прилёт (в секундах). Сузили до 30 минут: ракеты и
# шахеды долетают быстрее, а широкое окно ловило несвязанные пары (утром пустили —
# ночью был взрыв = ложная «пара»), что давало бредовые ETA в 40-60 минут.
PAIR_WINDOW = 30 * 60

# Окно выборки истории (дней). Чем больше — тем статистика устойчивее.
HISTORY_DAYS = 30

# Минимум пар, чтобы выдать оценку ETA. Меньше — говорим «недостаточно данных».
MIN_PAIRS = 3

# Типы угроз, которые считаем «в полёте» (пуском). Только явные пуски ракет и
# дронов — артиллерия и «прочее» сюда не относятся (они не «летят», а бьют).
LAUNCH_TYPES = ("missile", "uav")
# Типы, которые считаем «прилётом» (факт поражения). Только явные взрывы —
# «прочее» (other) убрано, т.к. короткие посты без контекста создавали шум.
IMPACT_TYPES = ("explosion",)

# === Каскад по threat_events (нормализованный журнал) =====================
# Мониторные каналы публикуют пуски стабильно, а прилёты — редко, поэтому
# пар по threats может не набраться месяцами. Каскад ниже даёт оценку из
# нормализованных событий (пары классов) и вероятность прилёта, а если и их
# мало — честную справку по типовому подлёту класса оружия.

# Классы оружия-пускачи и классы-исходы в threat_events.
EVENT_LAUNCH_CLASSES = ("uav", "shahed", "fpv", "missile", "cruise_missile", "ballistic", "kab")
EVENT_IMPACT_CLASSES = ("explosion", "air_defense")

# Окно поиска пары пуск→прилёт по threat_events (сек). Шире, чем по threats:
# движение БПЛА по территории региона занимает до часа.
EVENT_PAIR_WINDOW = 60 * 60

# Минимум пар/триггеров для каскадных оценок (честные пороги).
EVENT_MIN_PAIRS = 5
EVENT_MIN_TRIGGERS = 12

# Горизонт вероятности прилёта после пуска (сек).
EVENT_RISK_HORIZON = 30 * 60


def estimate_eta(db: Database, region: str, weapon_type: str | None = None) -> dict[str, Any]:
    """Оценка ETA для региона по истории пусков и прилётов.

    weapon_type: если указан ('missile' или 'uav') — считает ETA только для
    этого типа. Если None — смешанный (старое поведение, менее точно).
    Баллистика/ракета и БПЛА имеют радикально разную скорость, поэтому
    раздельный расчёт критичен.

    Возвращает словарь:
      {
        "available": bool,            # достаточно ли данных для оценки
        "avg_seconds": float | None,  # среднее время прилёта (если available)
        "samples": int,               # число пар пуск→прилёт в выборке
        "last_launch_ts": int | None  # ts последнего пуска в регионе (если есть)
      }
    """
    since = int(time.time()) - HISTORY_DAYS * 86400
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"available": False, "avg_seconds": None, "samples": 0, "last_launch_ts": None}

        # Пуски: если weapon_type задан — только этот тип; иначе оба (legacy).
        if weapon_type:
            launches = conn.execute(
                "SELECT ts FROM threats WHERE region = ? AND threat_type = ? "
                "AND ts >= ? ORDER BY ts",
                (region, weapon_type, since),
            ).fetchall()
        else:
            launches = conn.execute(
                "SELECT ts FROM threats WHERE region = ? AND threat_type IN (?, ?) "
                "AND ts >= ? ORDER BY ts",
                (region, "missile", "uav", since),
            ).fetchall()
        # Прилёты (взрывы) — всегда explosion, независимо от типа пуска.
        impact_ph = ",".join("?" * len(IMPACT_TYPES))
        impacts = conn.execute(
            f"SELECT ts FROM threats WHERE region = ? AND threat_type IN ({impact_ph}) "
            "AND ts >= ? ORDER BY ts",
            (region, *IMPACT_TYPES, since),
        ).fetchall()

        impact_ts = [row["ts"] for row in impacts]
        pairs: list[int] = []  # разницы ts в секундах
        for launch in launches:
            lts = launch["ts"]
            # Ближайший прилёт после пуска в пределах PAIR_WINDOW.
            for its in impact_ts:
                delta = its - lts
                if 0 < delta <= PAIR_WINDOW:
                    pairs.append(delta)
                    break  # один пуск → один ближайший прилёт

        last_launch_ts = launches[-1]["ts"] if launches else None
        if len(pairs) < MIN_PAIRS:
            return {
                "available": False,
                "avg_seconds": None,
                "samples": len(pairs),
                "last_launch_ts": last_launch_ts,
            }

        avg = sum(pairs) / len(pairs)
        return {
            "available": True,
            "avg_seconds": avg,
            "samples": len(pairs),
            "last_launch_ts": last_launch_ts,
        }
    except sqlite3.Error as exc:
        logger.warning("ETA: ошибка чтения БД для региона %s: %s", region, exc)
        return {"available": False, "avg_seconds": None, "samples": 0, "last_launch_ts": None}


def format_eta(estimate: dict[str, Any], region_name: str) -> str:
    """Человекочитаемое описание оценки ETA для сообщения бота."""
    if estimate["available"] and estimate["avg_seconds"] is not None:
        mins = estimate["avg_seconds"] / 60
        if mins < 60:
            time_str = f"~{mins:.0f} минут"
        else:
            time_str = f"~{mins / 60:.1f} часов"
        return (
            f"⏱ <b>{region_name}</b> — ETA угрозы\n\n"
            f"Среднее время прилёта (по истории): <b>{time_str}</b>\n"
            f"Основано на {estimate['samples']} случаях за {HISTORY_DAYS} дней.\n\n"
            f"⚠️ Это статистическая оценка, а не точный прогноз."
        )

    # Недостаточно данных — честно об этом говорим.
    samples = estimate.get("samples", 0)
    return (
        f"⏱ <b>{region_name}</b> — ETA угрозы\n\n"
        f"Недостаточно данных для оценки (нужно {MIN_PAIRS} пар пуск→прилёт, "
        f"сейчас: {samples}).\n"
        f"Бот копит статистику — через несколько дней/недель оценка появится.\n\n"
        f"Последний зафиксированный пуск: "
        f"{'был' if estimate.get('last_launch_ts') else 'не зафиксирован'}."
    )


# =====================================================================
#  Каскад по threat_events + итоговый текст для кнопки «ETA угроз»
# =====================================================================

def _quantile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = int(position), int(-(-position // 1))  # ceil
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _event_pair_rows(db: Database, region: str) -> tuple[list, list]:
    """Триггеры (пуски/движение) и исходы (прилёты/ППО) из threat_events."""
    conn = db._conn  # type: ignore[attr-defined]
    if conn is None:
        return [], []
    since = int(time.time()) - HISTORY_DAYS * 86400
    lph = ",".join("?" * len(EVENT_LAUNCH_CLASSES))
    iph = ",".join("?" * len(EVENT_IMPACT_CLASSES))
    launches = conn.execute(
        f"SELECT event_ts FROM threat_events WHERE region = ? AND stage IN ('launch', 'movement') "
        f"AND weapon_class IN ({lph}) AND event_ts >= ? ORDER BY event_ts",
        (region, *EVENT_LAUNCH_CLASSES, since),
    ).fetchall()
    outcomes = conn.execute(
        f"SELECT event_ts FROM threat_events WHERE region = ? "
        f"AND (stage = 'impact' OR weapon_class IN ({iph})) AND event_ts >= ? ORDER BY event_ts",
        (region, *EVENT_IMPACT_CLASSES, since),
    ).fetchall()
    return launches, outcomes


def _match_pairs(launches: list, outcomes: list, window: int) -> tuple[list[float], int]:
    """Один пуск → один ближайший исход в окне. Возвращает (задержки, успехи)."""
    impact_ts = [row["event_ts"] for row in outcomes]
    used: set[int] = set()
    delays: list[float] = []
    successes = 0
    for launch in launches:
        lts = launch["event_ts"]
        for i, its in enumerate(impact_ts):
            delta = its - lts
            if 0 < delta <= window:
                if i not in used:
                    delays.append(float(delta))
                    used.add(i)
                successes += 1
                break
    return delays, successes


def estimate_event_eta(db: Database, region: str) -> dict[str, Any]:
    """Медиана задержки пуск→прилёт по нормализованному журналу threat_events.

    Работает на классах оружия: триггер — launch/movement пускача, исход —
    impact либо explosion/ППО. Порог честный: EVENT_MIN_PAIRS пар.
    """
    try:
        launches, outcomes = _event_pair_rows(db, region)
        delays, _ = _match_pairs(launches, outcomes, EVENT_PAIR_WINDOW)
        if len(delays) < EVENT_MIN_PAIRS:
            return {"available": False, "samples": len(delays), "triggers": len(launches)}
        return {
            "available": True,
            "samples": len(delays),
            "triggers": len(launches),
            "median_seconds": _quantile(delays, 0.5),
            "p20_seconds": _quantile(delays, 0.2),
            "p80_seconds": _quantile(delays, 0.8),
        }
    except sqlite3.Error as exc:
        logger.warning("ETA events: ошибка чтения БД для региона %s: %s", region, exc)
        return {"available": False, "samples": 0, "triggers": 0}


def estimate_event_risk(db: Database, region: str) -> dict[str, Any]:
    """P(прилёт ≤ EVENT_RISK_HORIZON | пуск) со сглаживанием Лапласа.

    Вероятность, а не время: работает даже когда точных пар мало, но пусков
    достаточно (EVENT_MIN_TRIGGERS).
    """
    try:
        launches, outcomes = _event_pair_rows(db, region)
        _, successes = _match_pairs(launches, outcomes, EVENT_RISK_HORIZON)
        total = len(launches)
        if total < EVENT_MIN_TRIGGERS:
            return {"available": False, "samples": total, "successes": successes}
        return {
            "available": True,
            "samples": total,
            "successes": successes,
            "probability": (successes + 1) / (total + 2),
            "horizon_minutes": EVENT_RISK_HORIZON // 60,
        }
    except sqlite3.Error as exc:
        logger.warning("ETA risk: ошибка чтения БД для региона %s: %s", region, exc)
        return {"available": False, "samples": 0, "successes": 0}


def _fmt_minutes(seconds: float) -> str:
    mins = seconds / 60
    return f"~{mins:.0f} хв" if mins < 60 else f"~{mins / 60:.1f} год"


def vector_eta_text(fact, now_ts: int | None = None) -> str:
    """ETA по вектору «з X на Y»: конкретное число минут, а не типовой диапазон.

    Считается из расстояния между центроидами (city_coords.REGION_CENTROIDS)
    и средней скорости класса (weapon_classes.weapon_speed_kmh). Публикуется
    только при полной определённости: известны обе точки вектора, класс
    скорости есть, полученное время в реалистичном коридоре 2–90 минут
    (вне коридора оценка бессмысленна — оставляем типовой диапазон).

    Возвращает готовую строку для поста ('' — расчёт не удался, строку не
    добавляем). Пометка «орієнтовно» обязательна (правило ETA проекта).
    """
    from city_coords import REGION_CENTROIDS, _haversine_km
    from regions import region_name
    from weapon_classes import weapon_speed_kmh

    origin = getattr(fact, "origin_region", "")
    destination = getattr(fact, "destination_region", "")
    weapon = getattr(fact, "weapon_class", "")
    if not origin or not destination:
        return ""
    if origin in ("unknown", "multi") or destination in ("unknown", "multi"):
        return ""
    src = REGION_CENTROIDS.get(origin)
    dst = REGION_CENTROIDS.get(destination)
    # Профиль подтипа (реактивний БпЛА) важнее класса: 550 км/ч вместо 180.
    speed = weapon_speed_kmh(weapon, getattr(fact, "speed_profile", ""))
    if not src or not dst or speed <= 0:
        return ""
    # Пуск из региона цели — вектора нет, «ETA по вектору» не имеет смысла.
    if src == dst:
        return ""
    km = _haversine_km(src, dst)
    # Путь по земле длиннее прямой: поправка 1.25 (типовой коэффициент маршрута).
    minutes = (km * 1.25) / speed * 60.0
    if not (2 <= minutes <= 90):
        return ""
    human = f"≈{minutes:.0f} хв" if minutes < 60 else f"≈{minutes / 60:.1f} год"
    return (
        f"⏱ Розрахунок по вектору {region_name(origin)} → {region_name(destination)}: "
        f"{human} (орієнтовно, {km:.0f} км)"
    )


def build_eta_text(db: Database, region: str, name: str) -> str:
    """Каскад для кнопки «⏱ ETA угроз»: история → события → риск → справка.

    Каждая ступень помечена как статистическая оценка (правило проекта:
    прогнозы только с пометкой «орієнтовно/статистична оцінка»).
    """
    # 1) Исторические пары пуск→прилёт по журналу threats.
    est = estimate_eta(db, region)
    if est["available"] and est["avg_seconds"] is not None:
        return format_eta(est, name)

    # 2) Медиана по нормализованным событиям (классы оружия).
    ev = estimate_event_eta(db, region)
    if ev["available"]:
        return (
            f"⏱ <b>{name}</b> — ETA угрози (орієнтовно)\n\n"
            f"Медіана підльоту після пуску: <b>{_fmt_minutes(ev['median_seconds'])}</b>\n"
            f"Типовий коридор: {_fmt_minutes(ev['p20_seconds'])} – {_fmt_minutes(ev['p80_seconds'])}\n"
            f"Основано на {ev['samples']} парах подій за {HISTORY_DAYS} днів.\n\n"
            f"⚠️ Статистична оцінка, не точний прогноз."
        )

    # 3) Вероятность прилёта после пуска (когда пар мало, но пусков много).
    risk = estimate_event_risk(db, region)
    if risk["available"]:
        pct = round(risk["probability"] * 100)
        return (
            f"⏱ <b>{name}</b> — оцінка загрози (статистична)\n\n"
            f"Ймовірність прилёту упродовж {risk['horizon_minutes']} хв після "
            f"зафіксованого пуску: <b>≈{pct}%</b>\n"
            f"Статистика: {risk['successes']} з {risk['samples']} пусків за "
            f"{HISTORY_DAYS} днів.\n\n"
            f"⚠️ Статистична оцінка, не точний прогноз."
        )

    # 4) Справка по типовому подлёту класса оружия — когда данных ещё нет.
    from weapon_classes import weapon_eta, weapon_label
    ref_rows = []
    for slug in ("uav", "shahed", "cruise_missile", "ballistic"):
        ref_rows.append(f"  • {weapon_label(slug)}: {weapon_eta(slug)}")
    samples = est.get("samples", 0)
    return (
        f"⏱ <b>{name}</b> — ETA угрози\n\n"
        f"Недостатньо даних для регіональної оцінки "
        f"(потрібно {EVENT_MIN_PAIRS}+ подій, зараз: {samples} пар).\n"
        f"Бот накопичує статистику — оцінка з'явиться пізніше.\n\n"
        f"📖 Довідково, типовий підліт (орієнтовно):\n" + "\n".join(ref_rows) + "\n\n"
        f"⚠️ Довідкові значення, не прогноз для конкретної загрози."
    )
