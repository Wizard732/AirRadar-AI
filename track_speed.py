"""track_speed.py — вектор скорости цели по засечкам из threat_events.

Зачем: ETA по вектору «з X на Y» (eta.vector_eta_text) считает по центроидам
областей — грубо. Реальные мониторные каналы публикуют цепочку засечек
(«БпЛА отмечен в Конотопе» → «на подходе к Броварам»), и по ней можно
посчитать фактические курс и скорость.

Алгоритм:
  1. Берём события threat_events региона за окно (stage launch/movement,
     тот же класс оружия).
  2. Каждую засечку геокодируем по тексту события через detect_city:
     упомянутый населённый пункт = точка (lat, lon, ts).
  3. Нужны ≥2 РАЗНЫЕ точки (повтор города движения не показывает).
  4. speed_kmh = расстояние / время между первой и последней засечкой;
     course_deg — азимут (0° = север, по часовой).
  5. Реалистичный коридор скорости — иначе трек бракуется (дубликаты
     постов или «перескоки» по несвязанным городам дают бредовые скорости).

Никогда не публикуем без пометки «орієнтовно» (правило ETA проекта) и не
создаём новых таблиц — читаем только threat_events.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any

from city_coords import REGION_CENTROIDS, _haversine_km, city_by_slug, detect_city

logger = logging.getLogger(__name__)

# Окно поиска засечек (сек). Движение БПЛА через область занимает до часа,
# сводки выходят с интервалом в минуты — 2 часа покрывают полный пролёт.
TRACK_WINDOW_SECONDS = 2 * 3600

# Базовый коридор (профиль "") — реактивный подтип сужает его снизу.
TRACK_SPEED_MIN_KMH = 15.0
TRACK_SPEED_MAX_KMH = 1500.0

# Реалистичный коридор путевой скорости по засечкам (км/ч), по профилю
# подтипа. Ниже нижней границы — засечки в одном городе (дубликаты), выше
# верхней — несвязанные точки (ballistic ~2200 по городам не подтверждается
# засечками, такие треки бракуем). Реактивный подтип («реактивний БпЛА»)
# летит втрое быстрее поршневого: медленная цепочка для реактивной цели
# означает смешанные/бракованные засечки — такой трек не публикуем.
TRACK_SPEED_RANGES: dict[str, tuple[float, float]] = {
    "": (TRACK_SPEED_MIN_KMH, TRACK_SPEED_MAX_KMH),
    "reactive": (250.0, TRACK_SPEED_MAX_KMH),
}

# Коридор ETA по треку (мин), как в eta.vector_eta_text: вне — оценка
# бессмысленна, строку не публикуем.
TRACK_ETA_MIN, TRACK_ETA_MAX = 2, 90

# Поправка на маршрут по земле (как в eta.vector_eta_text).
ROUTE_FACTOR = 1.25

# Стадии событий, из которых берутся засечки.
_TRACK_STAGES = ("launch", "movement")


def _unavailable(points: int = 0) -> dict[str, Any]:
    return {
        "available": False,
        "points": points,
        "from_name": "", "to_name": "",
        "from": None, "to": None,
        "km": 0.0, "speed_kmh": 0.0, "course_deg": 0,
        "minutes": None, "region": "",
    }


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> int:
    """Азимут a → b в градусах: 0° = север, 90° = восток (по часовой)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return int(round(math.degrees(math.atan2(x, y))) % 360)


def build_track(
    db, region: str, weapon_class: str, event_ts: int,
    window_seconds: int = TRACK_WINDOW_SECONDS,
    speed_profile: str = "",
) -> dict[str, Any]:
    """Вектор скорости по ≥2 засечкам из threat_events + detect_city.

    region — регион-цель (fact.destination_region); его события и есть
    полёт к цели. speed_profile ('' | 'reactive') выбирает коридор скорости
    подтипа; SQL и дедуп засечек от профиля не зависят — цепочки цельные.
    Возвращает словарь; available=False — вектора нет
    (меньше двух разных городов, нереалистичная скорость, пустая БД).
    """
    if not region or region in ("unknown", "multi"):
        return _unavailable()
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return _unavailable()
        rows = conn.execute(
            "SELECT event_ts, text FROM threat_events "
            f"WHERE region = ? AND weapon_class = ? AND stage IN ({','.join('?' * len(_TRACK_STAGES))}) "
            "AND event_ts BETWEEN ? AND ? ORDER BY event_ts",
            (region, weapon_class, *_TRACK_STAGES, event_ts - window_seconds, event_ts),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("track_speed: ошибка чтения БД (%s/%s): %s", region, weapon_class, exc)
        return _unavailable()

    # Засечки: город из текста события → точка. Повтор того же города
    # (репост) движения не добавляет — дедуп по координатам.
    sightings: list[tuple[int, tuple[float, float], str]] = []
    seen: set[tuple[float, float]] = set()
    for row in rows:
        slug = detect_city(row["text"] or "")
        if not slug:
            continue
        info = city_by_slug(slug)
        if not info:
            continue
        point = (info[2], info[3])
        if point in seen:
            continue
        seen.add(point)
        sightings.append((int(row["event_ts"]), point, info[0]))
    if len(sightings) < 2:
        return _unavailable(len(sightings))

    first, last = sightings[0], sightings[-1]
    hours = (last[0] - first[0]) / 3600.0
    if hours <= 0:
        return _unavailable(len(sightings))
    km = _haversine_km(first[1], last[1])
    speed = km / hours
    speed_min, speed_max = TRACK_SPEED_RANGES.get(speed_profile, TRACK_SPEED_RANGES[""])
    if not (speed_min <= speed <= speed_max):
        return _unavailable(len(sightings))

    # ETA по треку: от последней засечки до центра региона-цели, той же
    # фактической скоростью. Вне коридора — None (строку не публикуем).
    minutes: int | None = None
    dst = REGION_CENTROIDS.get(region)
    if dst:
        remaining = _haversine_km(last[1], dst) * ROUTE_FACTOR
        eta = remaining / speed * 60.0
        if TRACK_ETA_MIN <= eta <= TRACK_ETA_MAX:
            minutes = int(round(eta))
    return {
        "available": True,
        "points": len(sightings),
        "from_name": first[2],
        "to_name": last[2],
        "from": [first[1][0], first[1][1]],
        "to": [last[1][0], last[1][1]],
        "km": round(km, 1),
        "speed_kmh": round(speed, 1),
        "course_deg": bearing_deg(first[1], last[1]),
        "minutes": minutes,
        "region": region,
    }


def track_payload(track: dict[str, Any] | None) -> dict[str, Any] | None:
    """Компактный JSON-пейлоад для /api/threats и EMBEDDED_THREATS.

    None — трека нет (карта работает по старому фолбэку routeOf).
    """
    if not track or not track.get("available"):
        return None
    return {
        "from": track["from"],
        "to": track["to"],
        "from_name": track["from_name"],
        "to_name": track["to_name"],
        "speed_kmh": track["speed_kmh"],
        "course_deg": track["course_deg"],
        "minutes": track["minutes"],
        "points": track["points"],
    }


def track_eta_text(track: dict[str, Any] | None, now_ts: int | None = None) -> str:
    """Строка ETA по треку для шапки алерта; '' — строку не добавляем.

    Публикуется только при полной определённости: трек собран, ETA попал
    в коридор. Пометка «орієнтовно» обязательна (правило ETA проекта).
    """
    from regions import region_name

    if not track or not track.get("available") or track.get("minutes") is None:
        return ""
    return (
        f"⏱ За треком: {track['from_name']} → {track['to_name']}, "
        f"≈{track['minutes']} хв до {region_name(track['region'])} · "
        f"~{track['speed_kmh']:.0f} км/год (орієнтовно)"
    )
