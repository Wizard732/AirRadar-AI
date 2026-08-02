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
