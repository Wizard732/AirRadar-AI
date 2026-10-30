"""track_engine.py — треки шахедов из наблюдений threat_parser.

Каналы дают снимки («Рух шахедів станом на 13:45: 4 на Сумщині — Суми, Низи…»),
а не координаты борта. Трек получается связыванием снимков во времени: цель
из нового снимка приписывается ближайшему треку, если расстояние достижимо
за прошедшее время и курс не разворачивается. Скорость и курс — по смещению
между снимками, а не выдумываются: нет двух точек — скорость «типовая»
(помечается), курса нет.

Модуль чистый (без БД и сети): принимает строки track_observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import gazetteer
from kyiv_time import _utc_offset_hours, kyiv_datetime
from datetime import datetime, timedelta, timezone

EARTH_KM = 6371.0
LINK_MAX_GAP_S = 25 * 60        # дольше без новых точек — трек закрыт
STALE_AFTER_S = 25 * 60         # «давно не видели» — рисуем серым, прогноз не даём
MIN_SEGMENT_KM = 3.0            # короче — шум геокодинга, скорость не меряем
MIN_SEGMENT_S = 120
SLACK_KM = 8.0                  # допуск на неточность геокодинга при связывании
TYPICAL_KMH = {"shahed": 180.0, "uav": 180.0, "cruise_missile": 800.0, "ballistic": 2000.0, "kab": 600.0}
REACTIVE_KMH = 550.0
SPEED_BOUNDS = {"": (60.0, 330.0), "reactive": (250.0, 750.0)}
# Типы оружия, которые ведём как треки (остальное — точечные события).
TRACKED = {"shahed", "uav", "cruise_missile", "ballistic"}
# Макс. скорость при связывании снимков (км/ч): баллистика и ракеты летят на порядок быстрее дронов.
LINK_VMAX = {"ballistic": 3000.0, "cruise_missile": 1000.0}
POSITION_RELATIONS = {"at", "near"}


# --- геометрия -------------------------------------------------------------

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlo = math.radians(b[1] - a[1])
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(a: tuple[float, float], bearing: float, km: float) -> tuple[float, float]:
    la1, lo1, br, d = math.radians(a[0]), math.radians(a[1]), math.radians(bearing), km / EARTH_KM
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(br))
    lo2 = lo1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


_COMPASS = ("північ", "північний схід", "схід", "південний схід", "південь", "південний захід", "захід", "північний захід")


def compass_ua(deg: float) -> str:
    return _COMPASS[int((deg % 360 + 22.5) // 45) % 8]


# --- модель ----------------------------------------------------------------

@dataclass
class Point:
    ts: int
    lat: float
    lon: float
    count: int | None
    place: str
    precision: str


@dataclass
class Track:
    id: str
    weapon: str
    profile: str
    points: list[Point] = field(default_factory=list)
    speed_kmh: float = 0.0
    speed_source: str = "typical"      # measured | typical
    heading: float | None = None
    status: str = "active"             # active | stale | loitering
    aim: float | None = None           # курс из «звідки → куди», если замеров смещения нет
    dest: tuple | None = None          # (lat, lon, name) — «курсом на X», если последнее сообщение так говорит
    course_to: str = ""                # цель из текста («курсом на Обухів»)

    @property
    def last(self) -> Point:
        return self.points[-1]

    @property
    def key(self) -> str:
        """Стабильный ключ: перестройка треков из тех же наблюдений даёт тот же ключ."""
        f = self.points[0]
        return f"{self.weapon}:{f.ts}:{f.lat:.2f}:{f.lon:.2f}"

    @property
    def count(self) -> int:
        return max((p.count or 1) for p in self.points[-1:])


def obs_timestamp(row: dict) -> int:
    """Время наблюдения: «станом на HH:MM» (киевское) важнее времени публикации."""
    event_ts = int(row["event_ts"])
    as_of = row.get("as_of") or ""
    if not as_of or ":" not in as_of:
        return event_ts
    hh, mm = (int(x) for x in as_of.split(":")[:2])
    kd = kyiv_datetime(event_ts)
    local = kd.replace(hour=hh, minute=mm, second=0, microsecond=0)
    utc = (local - timedelta(hours=_utc_offset_hours(datetime.fromtimestamp(event_ts, tz=timezone.utc)))).replace(
        tzinfo=timezone.utc)
    ts = int(utc.timestamp())
    if ts > event_ts + 300:      # «станом на 23:50» в посте после полуночи
        ts -= 86400
    return ts


def _speed_for(weapon: str, profile: str) -> float:
    return REACTIVE_KMH if profile == "reactive" else TYPICAL_KMH.get(weapon, 180.0)


TOWARD_BACK_KM = 25.0   # «курсом на X»: цель где-то на подходе, не в самой точке X


def _toward_as_position(r: dict, origin: dict | None) -> dict:
    """«Курсом на Харків» → приблизительная позиция на подходе к Харкову.

    Сообщение называет пункт назначения, а не текущее положение. С известным «звідки»
    ставим цель на линии откуда→куда за TOWARD_BACK_KM до пункта, иначе в самом пункте
    (precision=approx — карта подписывает «≈», скорость по таким точкам не меряем)."""
    from threat_parser import locate
    geo = locate(r["place"], r.get("oblast", ""))
    name = geo.name if geo and geo.precision == "place" else r["place"]
    out = dict(r, relation="near", precision="approx", place=f"→ {name}",
               dest=(r["lat"], r["lon"], name))
    if origin is not None:
        brg = bearing_deg((origin["lat"], origin["lon"]), (r["lat"], r["lon"]))
        d = haversine_km((origin["lat"], origin["lon"]), (r["lat"], r["lon"]))
        back = min(TOWARD_BACK_KM, d * 0.4)
        out["lat"], out["lon"] = destination((r["lat"], r["lon"]), (brg + 180) % 360, back)
        out["aim"] = brg
    return out


def _nodes(rows: list[dict]) -> list[dict]:
    """Снимки: группировка по (источник, время), одинаковые места суммируются."""
    snaps: dict[tuple[str, int, str, str], dict] = {}
    origins = {(r.get("source", ""), r["event_ts"], r["weapon"]): r for r in rows
               if r["relation"] == "from" and r["lat"] is not None}
    for r in rows:
        if r["weapon"] not in TRACKED or r["precision"] not in ("place", "approx") or r["lat"] is None:
            continue
        if r["relation"] == "toward" and r["precision"] == "place":
            r = _toward_as_position(r, origins.get((r.get("source", ""), r["event_ts"], r["weapon"])))
        if r["relation"] not in POSITION_RELATIONS:
            continue
        ts = obs_timestamp(r)
        key = (r["weapon"], r["profile"], round(r["lat"], 3), round(r["lon"], 3), ts // 60)
        node = snaps.setdefault(key, {"ts": ts, "lat": r["lat"], "lon": r["lon"], "weapon": r["weapon"],
                                      "profile": r["profile"], "count": 0, "place": r["place"],
                                      "precision": r["precision"], "unknown_count": 0,
                                      "aim": r.get("aim"), "dest": r.get("dest")})
        if r["count"]:
            node["count"] += r["count"]
        else:
            node["unknown_count"] += 1
    out = list(snaps.values())
    for n in out:
        n["count"] = n["count"] or n["unknown_count"] or 1
    out.sort(key=lambda n: n["ts"])
    return out


def build_tracks(rows: list[dict], now_ts: int, window_s: int = 6 * 3600) -> list[Track]:
    """Строки track_observations (старые→новые) → список треков за окно."""
    nodes = [n for n in _nodes(rows) if n["ts"] >= now_ts - window_s]
    tracks: list[Track] = []
    open_tracks: list[Track] = []
    # Узлы одного снимка обрабатываем пачкой: каждый трек получает ≤1 узел на снимок.
    i = 0
    while i < len(nodes):
        batch = [nodes[i]]
        while i + len(batch) < len(nodes) and nodes[i + len(batch)]["ts"] // 90 == nodes[i]["ts"] // 90:
            batch.append(nodes[i + len(batch)])
        i += len(batch)
        bts = max(n["ts"] for n in batch)
        open_tracks = [t for t in open_tracks if bts - t.last.ts <= LINK_MAX_GAP_S]
        cands = []
        for t in open_tracks:
            for n in batch:
                if n["weapon"] != t.weapon and not ({n["weapon"], t.weapon} <= {"shahed", "uav"}):
                    continue
                dt = n["ts"] - t.last.ts
                dist = haversine_km((t.last.lat, t.last.lon), (n["lat"], n["lon"]))
                vmax = max(650.0 if (t.profile == "reactive" or n["profile"] == "reactive") else 280.0,
                           LINK_VMAX.get(t.weapon, 0.0))
                if dt <= 0:
                    if dist > SLACK_KM * 2:
                        continue
                elif dist > vmax * dt / 3600 + SLACK_KM:
                    continue
                cost = dist
                if t.heading is not None and dist >= 15.0 and angle_diff(
                        t.heading, bearing_deg((t.last.lat, t.last.lon), (n["lat"], n["lon"]))) > 110:
                    continue   # цілі не розвертаються на 180°: це інша ціль, а не продовження траси
                if t.heading is not None and dist >= MIN_SEGMENT_KM:
                    cost += angle_diff(t.heading, bearing_deg((t.last.lat, t.last.lon), (n["lat"], n["lon"]))) * 0.4
                cands.append((cost, id(t), id(n), t, n))
        cands.sort(key=lambda c: c[0])
        used_t: set[int] = set()
        used_n: set[int] = set()
        for _, _, _, t, n in cands:
            if id(t) in used_t or id(n) in used_n:
                continue
            used_t.add(id(t))
            used_n.add(id(n))
            _append(t, n)
        for n in batch:
            if id(n) in used_n:
                continue
            t = Track(id=f"t{len(tracks) + 1}", weapon=n["weapon"], profile=n["profile"])
            _append(t, n)
            tracks.append(t)
            open_tracks.append(t)
    for t in tracks:
        _finalize(t, now_ts)
    return tracks


def _append(t: Track, n: dict) -> None:
    t.points.append(Point(n["ts"], n["lat"], n["lon"], n["count"], n["place"], n["precision"]))
    if n["profile"] == "reactive":
        t.profile = "reactive"
    t.aim = n.get("aim")
    t.dest = n.get("dest")


def _finalize(t: Track, now_ts: int) -> None:
    """Скорость/курс по смещению. Сглаживаем по последним сегментам, а не по паре точек."""
    segs = []
    pts = t.points
    for a, b in zip(pts, pts[1:]):
        dt = b.ts - a.ts
        km = haversine_km((a.lat, a.lon), (b.lat, b.lon))
        if dt >= MIN_SEGMENT_S and km >= MIN_SEGMENT_KM and a.precision == "place" and b.precision == "place":
            segs.append((km, dt, bearing_deg((a.lat, a.lon), (b.lat, b.lon))))
    lo, hi = SPEED_BOUNDS.get(t.profile, SPEED_BOUNDS[""])
    if segs:
        recent = segs[-3:]
        km = sum(s[0] for s in recent)
        dt = sum(s[1] for s in recent)
        speed = km / (dt / 3600)
        # Круговое среднее курса
        sx = sum(math.sin(math.radians(s[2])) * s[0] for s in recent)
        cy = sum(math.cos(math.radians(s[2])) * s[0] for s in recent)
        t.heading = (math.degrees(math.atan2(sx, cy)) + 360) % 360
        if lo <= speed <= hi:
            t.speed_kmh, t.speed_source = round(speed, 0), "measured"
        else:
            # Скорость вне коридора — сдвиг связан с геокодингом/другой целью: берём типовую.
            t.speed_kmh, t.speed_source = _speed_for(t.weapon, t.profile), "typical"
    else:
        t.speed_kmh, t.speed_source = _speed_for(t.weapon, t.profile), "typical"
        if t.aim is not None:
            t.heading = t.aim
        elif len(pts) >= 2:
            t.status = "loitering"
    if now_ts - t.last.ts > STALE_AFTER_S:
        t.status = "stale"


# --- положение и прогноз -----------------------------------------------------

def position_at(t: Track, ts: int) -> tuple[float, float, bool]:
    """(lat, lon, estimated). Внутри трека — интерполяция, после последней точки —
    экстраполяция по курсу не дольше 20 минут."""
    pts = t.points
    if ts <= pts[0].ts:
        return pts[0].lat, pts[0].lon, False
    for a, b in zip(pts, pts[1:]):
        if a.ts <= ts <= b.ts:
            k = 0 if b.ts == a.ts else (ts - a.ts) / (b.ts - a.ts)
            return a.lat + (b.lat - a.lat) * k, a.lon + (b.lon - a.lon) * k, False
    last = pts[-1]
    if t.heading is None or t.status != "active":
        return last.lat, last.lon, False
    dt = min(ts - last.ts, 20 * 60)
    lat, lon = destination((last.lat, last.lon), t.heading, t.speed_kmh * dt / 3600)
    return lat, lon, True


@dataclass
class PathPlace:
    name: str
    lat: float
    lon: float
    eta_min: int
    km: float


def forecast_places(t: Track, now_ts: int, horizon_min: int = 60, corridor_km: float = 12.0,
                    min_population: int = 5000, limit: int = 4) -> list[PathPlace]:
    """Города в коридоре по курсу, с оценкой времени. Только для активных треков с курсом."""
    if t.heading is None or t.status != "active":
        return []
    lat0, lon0, _ = position_at(t, now_ts)
    here = (lat0, lon0)
    reach = t.speed_kmh * horizon_min / 60
    out: list[PathPlace] = []
    for town in gazetteer.towns(min_population):
        p = (town.lat, town.lon)
        dist = haversine_km(here, p)
        if dist < 1 or dist > reach:
            continue
        off = angle_diff(t.heading, bearing_deg(here, p))
        if off > 60:
            continue
        cross = dist * math.sin(math.radians(off))
        if cross > corridor_km:
            continue
        along = dist * math.cos(math.radians(off))
        out.append(PathPlace(town.name, town.lat, town.lon, max(1, round(along / t.speed_kmh * 60)), round(dist, 1)))
    out.sort(key=lambda p: p.eta_min)
    return out[:limit]


# --- риск для точки ---------------------------------------------------------

@dataclass
class Risk:
    track_id: str
    weapon: str
    level: str          # over | imminent | watch
    distance_km: float
    bearing_from_point: float   # откуда цель (азимут от точки на цель)
    approaching: bool
    cpa_km: float
    eta_min: int | None
    speed_kmh: float
    speed_source: str
    count: int
    eta_range: tuple[int, int] | None = None   # (раньше, позже) в минутах — честный разброс вместо одного числа


LEAD_MIN = 20            # предупреждаем не ближе, чем за ~20 мин лёта: быстрая цель видна издалека
IMMINENT_ETA_MIN = 8     # быстрая цель (ракета, реактивный) на курсе и прилёт ≤8 мин — «imminent», сколько бы км ни было
FAST_KMH = 400.0
UNKNOWN_COURSE_MULT = 2  # цель без курса: «watch» в пределах radius×2 (не молчим, но и не за 30 км)
REACH_CAP_KM = 300.0


def _eta_range(eta: int, speed_source: str) -> tuple[int, int]:
    """Разброс ETA: замеренная скорость точнее типовой; «раньше» важнее «позже» — не обнадёживаем."""
    fast, slow = (1.15, 0.85) if speed_source == "measured" else (1.35, 0.7)
    return max(0, int(eta / fast)), max(1, math.ceil(eta / slow))


def risk_for_point(tracks: list[Track], point: tuple[float, float], now_ts: int,
                   imminent_km: float = 10.0, watch_km: float = 30.0) -> list[Risk]:
    """Для каждой активной цели: расстояние, сближается ли, ближайшая точка подхода и ETA.

    Дальность наблюдения зависит от скорости (LEAD_MIN минут лёта), допуск на курс растёт с
    расстоянием (ошибка геокодинга и курса), цель без курса не игнорируется."""
    risks: list[Risk] = []
    for t in tracks:
        if t.status == "stale":
            continue
        lat, lon, _ = position_at(t, now_ts)
        here = (lat, lon)
        dist = haversine_km(here, point)
        reach = min(REACH_CAP_KM, max(watch_km, imminent_km, t.speed_kmh * LEAD_MIN / 60))
        if dist > reach:
            continue
        brg = bearing_deg(point, here)
        approaching, cpa, eta = False, dist, None
        has_course = t.heading is not None and t.speed_kmh > 0
        if has_course:
            to_point = bearing_deg(here, point)
            off = angle_diff(t.heading, to_point)
            approaching = off < 75
            cpa = abs(dist * math.sin(math.radians(off))) if off < 90 else dist
            if approaching:
                along = dist * math.cos(math.radians(off))
                eta = max(0, round(along / t.speed_kmh * 60))
        margin = 3.0 + 0.12 * dist     # курс по снимкам неточен (~7°) + геокодинг места
        approx_only = t.last.precision == "approx" and t.dest is not None   # «курсом на X»: реальної позиції немає
        if approx_only and not approaching and dist <= max(3.0, imminent_km):
            approaching = True      # названа ціль «курсом на X» поруч із вами — це і є курс на вас
            eta = round(dist / t.speed_kmh * 60) if t.speed_kmh > 0 else None
        if dist <= 3.0 and not approx_only:
            level = "over"
        elif approx_only and dist <= max(3.0, imminent_km):
            level = "imminent"
        elif dist <= imminent_km and (approaching or not has_course):
            level = "imminent"
        elif approaching and t.speed_kmh >= FAST_KMH and cpa <= imminent_km + margin and eta is not None and eta <= IMMINENT_ETA_MIN:
            level = "imminent"
        elif approaching and dist <= reach and cpa <= imminent_km + margin:
            level = "watch"
        elif not has_course and dist <= imminent_km * UNKNOWN_COURSE_MULT:
            level = "watch"
        else:
            continue
        rng = _eta_range(eta, t.speed_source) if eta is not None and level != "over" else None
        risks.append(Risk(t.id, t.weapon, level, round(dist, 1), round(brg), approaching, round(cpa, 1),
                          eta, t.speed_kmh, t.speed_source, t.count, rng))
    risks.sort(key=lambda r: r.distance_km)
    return risks


def _eta_phrase(r: Risk) -> str:
    if r.eta_min is None:
        return ""
    if r.eta_range and r.eta_range[0] != r.eta_range[1]:
        return f"орієнтовно {r.eta_range[0]}–{r.eta_range[1]} хв"
    return f"орієнтовно {r.eta_min} хв"


def risk_text(r: Risk) -> str:
    """Одна строка без воды: что, где, куда, как быстро. «Орієнтовно» — всегда."""
    what = "Реактивний БпЛА" if r.weapon == "uav" and r.speed_kmh >= 250 else (
        "Шахед" if r.weapon == "shahed" else "БпЛА" if r.weapon == "uav" else
        "Балістика" if r.weapon == "ballistic" else "Крилата ракета")
    if r.count > 1:
        what = f"{r.count} цілі"
    if r.level == "over":
        return f"🔴 {what} над вами або поруч (~{r.distance_km:g} км)."
    side = compass_ua(r.bearing_from_point)
    speed = f"~{r.speed_kmh:g} км/год" + ("" if r.speed_source == "measured" else ", типова")
    eta = _eta_phrase(r)
    if r.level == "imminent":
        course = "курс на вас" if r.approaching else "курс невідомий — вважайте, що на вас"
        return f"🟠 {what}: {r.distance_km:g} км, {side}, {course}" + (f", {eta}" if eta else "") + f" ({speed})."
    if not r.approaching:
        return f"🟡 {what}: {r.distance_km:g} км, {side}, курс невідомий, орієнтовно — слідкуйте ({speed})."
    return f"🟡 {what}: {r.distance_km:g} км, {side}, може пройти за ~{r.cpa_km:g} км від вас, {eta} ({speed})."


# --- одиночные события: КАБ, ракеты, авиация, FPV, РСЗО… ---------------------------

EVENT_WEAPONS = frozenset({
    "kab", "ballistic", "missile", "air_missile", "coastal_missile", "aviation", "tac_aviation",
    "strat_aviation", "mlrs", "artillery", "fpv", "recon_drone", "explosion", "air_defense",
})
EVENT_WINDOW_S = 90 * 60


def build_events(rows: list[dict], now_ts: int, window_s: int = EVENT_WINDOW_S) -> list[dict]:
    """Не-«летящие» цели и мгновенные события для карты: точка, тип, время, область.

    Дубликаты (то же оружие в той же точке за 5 минут) схлопываются; число берём максимальное."""
    out: dict[tuple, dict] = {}
    items = []
    for r in rows:
        if r["weapon"] not in EVENT_WEAPONS or r["lat"] is None or r["precision"] == "none" or r["relation"] == "from":
            continue
        if r["weapon"] in TRACKED and r["precision"] in ("place", "approx"):
            continue      # уже ведётся как трек — второй маркер на карте не нужен
        ts = obs_timestamp(r)
        if ts < now_ts - window_s or ts > now_ts + 600:
            continue
        items.append((ts, r))
    for ts, r in sorted(items, key=lambda x: x[0]):
        key = (r["weapon"], round(r["lat"], 2), round(r["lon"], 2))
        e = out.get(key)
        if e is not None and ts - e["ts"] <= 300:      # те саме оружие в тій же точці в межах 5 хв — один запис
            e["count"] = max(e["count"], r["count"] or 1)
            e["ts"] = max(e["ts"], ts)
            continue
        out[key + (ts,)] = {"weapon": r["weapon"], "lat": round(r["lat"], 4), "lon": round(r["lon"], 4), "ts": ts,
                            "count": r["count"] or 1, "place": r["place"], "oblast": r["oblast"],
                            "precision": r["precision"], "toward": r["relation"] == "toward"}
        out[key] = out[key + (ts,)]
    uniq = {id(v): v for v in out.values()}
    return sorted(uniq.values(), key=lambda e: -e["ts"])
