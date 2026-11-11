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
    sources: set = field(default_factory=set)   # незалежні джерела, що бачили цю ціль (довіра)
    sigma: float = 25.0                # невизначеність курсу, ° (1σ): розкрив конуса прогнозу
    heading_source: str = ""           # fit | segments | aim | dest | wave | '' — звідки курс
    anchor: "Point | None" = None      # остання ТОЧНА позиція (для курсу «на X», де сама точка X — не положення цілі)

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
                                      "aim": r.get("aim"), "dest": r.get("dest"), "sources": set()})
        if r.get("source"):
            node["sources"].add(r["source"])
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
    _apply_wave_course(tracks, now_ts)
    return tracks


def _append(t: Track, n: dict) -> None:
    t.points.append(Point(n["ts"], n["lat"], n["lon"], n["count"], n["place"], n["precision"]))
    if n["profile"] == "reactive":
        t.profile = "reactive"
    t.aim = n.get("aim")
    t.dest = n.get("dest")
    t.sources |= n.get("sources", set())


def _anchor_toward_dest(t: Track) -> bool:
    """Є точна позиція (не «→ X») і названий пункт призначення ≥5 км від неї → курс на нього (σ≈14°)."""
    dlat, dlon = t.dest[0], t.dest[1]
    for p in reversed(t.points):
        if p.precision == "place" and haversine_km((p.lat, p.lon), (dlat, dlon)) >= 5.0:
            t.anchor, t.heading = p, bearing_deg((p.lat, p.lon), (dlat, dlon))
            t.sigma, t.heading_source = 14.0, "dest"
            return True
    return False


WAVE_RADIUS_KM, WAVE_MIN_NEIGHBORS, WAVE_MAX_SPREAD_DEG, WAVE_SIGMA = 180.0, 2, 35.0, 30.0


def _apply_wave_course(tracks: list[Track], now_ts: int) -> None:
    """Ціль без курсу (одна точка) у хвилі однотипних цілей: беремо спільний курс сусідів, якщо вони летять купно.

    Хвиля шахедів/ракет зазвичай має спільний напрям. Умови: ≥2 сусідів того ж типу в радіусі, їхній курс згуртований
    (розкид ≤35°), цілі свіжі. Позначається heading_source='wave', σ=30° — прогноз є, але впевненість низька."""
    donors = [t for t in tracks if t.heading is not None and t.status == "active" and t.heading_source in ("fit", "segments", "aim", "dest")]
    for t in tracks:
        if t.heading is not None or t.status != "active" or now_ts - t.last.ts > 15 * 60:
            continue
        near = [d for d in donors if d.weapon == t.weapon and haversine_km((d.last.lat, d.last.lon), (t.last.lat, t.last.lon)) <= WAVE_RADIUS_KM
                and abs(d.last.ts - t.last.ts) <= 20 * 60]
        if len(near) < WAVE_MIN_NEIGHBORS:
            continue
        sx = sum(math.sin(math.radians(d.heading)) for d in near)
        cy = sum(math.cos(math.radians(d.heading)) for d in near)
        r = math.hypot(sx, cy) / len(near)                      # довжина середнього вектора: 1 — усі в один бік
        mean = (math.degrees(math.atan2(sx, cy)) + 360) % 360
        spread = max(angle_diff(mean, d.heading) for d in near)
        if r < 0.85 or spread > WAVE_MAX_SPREAD_DEG:
            continue
        t.heading, t.sigma, t.heading_source = mean, WAVE_SIGMA, "wave"


FIT_MIN_POINTS, FIT_MIN_SPAN_S, FIT_MIN_DISP_KM, FIT_WINDOW_S, FIT_TAU_S = 3, 360, 6.0, 40 * 60, 900.0


def _fit_velocity(pts: list[Point]) -> tuple[float, float, float] | None:
    """Швидкість і курс за регресією по останніх точках (зважено: свіжіші важливіші) → (км/год, курс°, σ°).

    Усереднення двох сусідніх сегментів хитається від шуму геокодингу: одна неточна точка міняє курс на десятки
    градусів. Лінійна підгонка x(t), y(t) по 3–6 точках стабільніша, а розкид залишків дає чесну оцінку невизначеності."""
    place = [p for p in pts if p.precision == "place"]
    if len(place) < FIT_MIN_POINTS:
        return None
    last = place[-1]
    use = [p for p in place if last.ts - p.ts <= FIT_WINDOW_S][-6:]
    if len(use) < FIT_MIN_POINTS or use[-1].ts - use[0].ts < FIT_MIN_SPAN_S:
        return None
    kx, ky = 111.32 * math.cos(math.radians(last.lat)), 110.57
    xs = [(p.lon - last.lon) * kx for p in use]
    ys = [(p.lat - last.lat) * ky for p in use]
    ts = [(p.ts - last.ts) / 3600.0 for p in use]                 # години відносно останньої точки (≤ 0)
    ws = [math.exp(-(last.ts - p.ts) / FIT_TAU_S) for p in use]
    sw = sum(ws)
    mt = sum(w * t for w, t in zip(ws, ts)) / sw
    var = sum(w * (t - mt) ** 2 for w, t in zip(ws, ts))
    if var <= 1e-9:
        return None
    vx = sum(w * (t - mt) * (x - sum(w2 * x2 for w2, x2 in zip(ws, xs)) / sw) for w, t, x in zip(ws, ts, xs)) / var
    vy = sum(w * (t - mt) * (y - sum(w2 * y2 for w2, y2 in zip(ws, ys)) / sw) for w, t, y in zip(ws, ts, ys)) / var
    speed = math.hypot(vx, vy)
    disp = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0])
    if disp < FIT_MIN_DISP_KM or speed < 1.0:
        return None
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    mt2 = mt
    res = [math.hypot(x - (mx + vx * (t - mt2)), y - (my + vy * (t - mt2))) for x, y, t in zip(xs, ys, ts)]
    rms = math.sqrt(sum(w * r * r for w, r in zip(ws, res)) / sw)
    heading = (math.degrees(math.atan2(vx, vy)) + 360) % 360
    sigma = min(35.0, max(4.0, math.degrees(math.atan2(rms, max(disp, 1.0))) * 1.5 + 4.0))
    return speed, heading, sigma


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
    fit = _fit_velocity(pts)
    if fit is not None and lo <= fit[0] <= hi:
        # ≥3 точок: курс і швидкість за регресією (стабільніше за пару сегментів), σ — з розкиду залишків
        t.speed_kmh, t.speed_source = round(fit[0], 0), "measured"
        t.heading, t.sigma, t.heading_source = fit[1], fit[2], "fit"
    elif segs:
        recent = segs[-3:]
        km = sum(s[0] for s in recent)
        dt = sum(s[1] for s in recent)
        speed = km / (dt / 3600)
        # Круговое среднее курса
        sx = sum(math.sin(math.radians(s[2])) * s[0] for s in recent)
        cy = sum(math.cos(math.radians(s[2])) * s[0] for s in recent)
        t.heading = (math.degrees(math.atan2(sx, cy)) + 360) % 360
        t.sigma, t.heading_source = (14.0 if len(segs) == 1 else 10.0), "segments"
        if lo <= speed <= hi:
            t.speed_kmh, t.speed_source = round(speed, 0), "measured"
        else:
            # Скорость вне коридора — сдвиг связан с геокодингом/другой целью: берём типовую.
            t.speed_kmh, t.speed_source = _speed_for(t.weapon, t.profile), "typical"
    else:
        t.speed_kmh, t.speed_source = _speed_for(t.weapon, t.profile), "typical"
        if t.aim is not None:
            t.heading = t.aim
            t.sigma, t.heading_source = 12.0, "aim"
        elif t.dest is not None and _anchor_toward_dest(t):
            pass                                   # курс від останньої точної позиції до названого пункту
        elif len(pts) >= 2:
            t.status = "loitering"
    if now_ts - t.last.ts > STALE_AFTER_S:
        t.status = "stale"


# --- положение и прогноз -----------------------------------------------------

EXTRAPOLATE_MAX_S = 10 * 60     # далі 10 хв за курсом позиція — здогад, не малюємо її як факт


def position_at(t: Track, ts: int) -> tuple[float, float, bool]:
    """(lat, lon, estimated). Внутри трека — интерполяция, после последней точки —
    экстраполяция по курсу не дольше EXTRAPOLATE_MAX_S и не дальше пункта назначения.
    estimated=True и для приблизительной точки («курсом на X»): реальное положение неизвестно."""
    pts = t.points
    if ts <= pts[0].ts:
        return pts[0].lat, pts[0].lon, pts[0].precision == "approx"
    for a, b in zip(pts, pts[1:]):
        if a.ts <= ts <= b.ts:
            k = 0 if b.ts == a.ts else (ts - a.ts) / (b.ts - a.ts)
            return a.lat + (b.lat - a.lat) * k, a.lon + (b.lon - a.lon) * k, a.precision == "approx" or b.precision == "approx"
    last = pts[-1]
    if t.heading is None or t.status != "active":
        return last.lat, last.lon, last.precision == "approx"
    if t.heading_source == "dest" and t.anchor is not None and t.dest is not None:
        # остання точка = сам пункт призначення, а не положення цілі: летимо від останньої ТОЧНОЇ позиції і не далі пункту
        km = t.speed_kmh * max(0, ts - t.anchor.ts) / 3600
        cap = haversine_km((t.anchor.lat, t.anchor.lon), (t.dest[0], t.dest[1])) * 0.95
        lat, lon = destination((t.anchor.lat, t.anchor.lon), t.heading, min(km, cap))
        return lat, lon, True
    dt = max(0, min(ts - last.ts, EXTRAPOLATE_MAX_S))
    km = t.speed_kmh * dt / 3600
    if t.dest is not None:   # не «пролітаємо» повз заявлену ціль
        km = min(km, haversine_km((last.lat, last.lon), (t.dest[0], t.dest[1])))
    lat, lon = destination((last.lat, last.lon), t.heading, km)
    return lat, lon, True


def position_radius_km(t: Track, ts: int) -> float:
    """Радіус невизначеності позиції для карти (км); 0 — позиція точна."""
    last = t.points[-1]
    r = TOWARD_BACK_KM if last.precision == "approx" else 0.0
    if t.heading is not None and t.status == "active" and ts > last.ts:
        r = max(r, 5.0 + min(ts - last.ts, EXTRAPOLATE_MAX_S) / 60 * 1.5)
    return round(r, 1)


@dataclass
class PathPlace:
    name: str
    lat: float
    lon: float
    eta_min: int
    km: float
    eta_lo: int = 0
    eta_hi: int = 0
    conf: str = ""      # висока | середня | низька — наскільки місто близько до осі курсу (без відсотків)


HORIZON_BY_WEAPON = {"ballistic": 15, "cruise_missile": 45}


def forecast_places(t: Track, now_ts: int, horizon_min: int = 60, corridor_km: float = 12.0,
                    min_population: int = 5000, limit: int = 4) -> list[PathPlace]:
    """Міста в конусі за курсом: коридор розширюється з відстанню (σ курсу), ETA — діапазоном (σ швидкості).

    Тільки для активних треків із курсом. Чим менш певний курс (мало точок, шум), тим ширший конус, але й
    нижча «впевненість»: далеке місто на краю конуса не видається за певну ціль."""
    if t.heading is None or t.status != "active":
        return []
    lat0, lon0, _ = position_at(t, now_ts)
    here = (lat0, lon0)
    horizon_min = min(horizon_min, HORIZON_BY_WEAPON.get(t.weapon, horizon_min))
    reach = t.speed_kmh * horizon_min / 60
    sigma = max(4.0, t.sigma)
    tan_s = math.tan(math.radians(min(sigma, 45.0)))
    spd_unc = 0.2 if t.speed_source == "measured" else 0.4
    out: list[PathPlace] = []
    for town in gazetteer.towns(min_population):
        p = (town.lat, town.lon)
        dist = haversine_km(here, p)
        if dist < 1 or dist > reach:
            continue
        off = angle_diff(t.heading, bearing_deg(here, p))
        if off > max(60.0, sigma * 2):
            continue
        cross = dist * math.sin(math.radians(off))
        along = dist * math.cos(math.radians(off))
        if cross > corridor_km + max(0.0, along) * tan_s:
            continue
        eta = max(1, round(along / t.speed_kmh * 60))
        ratio = cross / (corridor_km + max(0.0, along) * tan_s)
        conf = "висока" if ratio <= 0.35 else ("середня" if ratio <= 0.7 else "низька")
        out.append(PathPlace(town.name, town.lat, town.lon, eta, round(dist, 1),
                             max(1, math.floor(eta * (1 - spd_unc))), max(1, math.ceil(eta * (1 + spd_unc))), conf))
    out.sort(key=lambda p: (p.eta_min, p.km))
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
    sources: int = 1                  # скільки незалежних джерел бачили ціль
    official: bool | None = None      # True — в районі цілі є офіційна тривога; None — офіційних даних немає
    pass_by: bool = False             # ціль пройде повз (MDA більший за похибку курсу), а не «на вас»
    approx_only: bool = False         # позиція невідома: названо лише ціль «курсом на X» — відстань/ETA не показуємо


LEAD_MIN = 20            # предупреждаем не ближе, чем за ~20 мин лёта: быстрая цель видна издалека
IMMINENT_ETA_MIN = 8     # быстрая цель (ракета, реактивный) на курсе и прилёт ≤8 мин — «imminent», сколько бы км ни было
FAST_KMH = 400.0
UNKNOWN_COURSE_MULT = 2  # цель без курса: «watch» в пределах radius×2 (не молчим, но и не за 30 км)
REACH_CAP_KM = 300.0


def _eta_range(eta: int, speed_source: str) -> tuple[int, int]:
    """Разброс ETA: замеренная скорость точнее типовой; «раньше» важнее «позже» — не обнадёживаем."""
    fast, slow = (1.15, 0.85) if speed_source == "measured" else (1.35, 0.7)
    return max(0, int(eta / fast)), max(1, math.ceil(eta / slow))


def _is_official(lat: float, lon: float, official: set | None) -> bool | None:
    """Чи є офіційна тривога там, де зараз ціль (район або область). None — офіційних даних немає."""
    if official is None:
        return None
    from raion_geo import raion_at
    ra = raion_at(lat, lon)
    if ra and (f"raion:{ra[0]}" in official or f"raionY:{ra[0]}" in official):
        return True
    from city_coords import region_for_point
    return region_for_point(lat, lon) in official


def risk_for_point(tracks: list[Track], point: tuple[float, float], now_ts: int,
                   imminent_km: float = 10.0, watch_km: float = 30.0, official: set | None = None) -> list[Risk]:
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
        pass_by = bool(approaching and has_course and not approx_only and level == "imminent" and cpa >= margin and cpa >= 3.0)
        risks.append(Risk(t.id, t.weapon, level, round(dist, 1), round(brg), approaching, round(cpa, 1),
                          eta, t.speed_kmh, t.speed_source, t.count, rng, max(1, len(t.sources)),
                          _is_official(lat, lon, official), pass_by, approx_only and level != "over"))
    risks.sort(key=lambda r: r.distance_km)
    return risks


def _eta_phrase(r: Risk) -> str:
    if r.eta_min is None:
        return ""
    if r.eta_range and r.eta_range[0] != r.eta_range[1]:
        return f"орієнтовно {r.eta_range[0]}–{r.eta_range[1]} хв"
    return f"орієнтовно {r.eta_min} хв"


def trust_phrase(r: Risk) -> str:
    """Скільки джерел і чи є офіційне підтвердження — чесно, без паніки і без заспокоєння."""
    if r.official is True:
        return " ✅ Підтверджено офіційною тривогою."
    if r.official is False:
        return (" ❔ Без офіційного підтвердження (1 джерело)." if r.sources < 2
                else f" ❔ Офіційної тривоги ще немає ({r.sources} джерела).")
    return ""


def risk_text(r: Risk) -> str:
    """Одна строка без воды: что, где, куда, как быстро. «Орієнтовно» — всегда."""
    what = "Реактивний БпЛА" if r.weapon == "uav" and r.speed_kmh >= 250 else (
        "Шахед" if r.weapon == "shahed" else "БпЛА" if r.weapon == "uav" else
        "Балістика" if r.weapon == "ballistic" else "Крилата ракета")
    if r.count > 1:
        what = f"{r.count} цілі"
    if r.level == "over":
        return f"🔴 {what} над вами або поруч (~{r.distance_km:g} км)." + trust_phrase(r)
    if r.approx_only:   # «курсом на Київ»: де саме ціль зараз — невідомо, тому «0 км / 0–1 хв» було б вигадкою
        return (f"🟠 {what} → ваш район · точна позиція невідома, вважайте курс на вас "
                f"(~{format(r.speed_kmh, 'g')} км/год{'' if r.speed_source == 'measured' else ', типова'})." + trust_phrase(r))
    side = compass_ua(r.bearing_from_point)
    speed = f"~{r.speed_kmh:g} км/год" + ("" if r.speed_source == "measured" else ", типова")
    eta = _eta_phrase(r)
    if r.level == "imminent":
        course = ("курс на вас" if not r.pass_by else f"пройде за ~{r.cpa_km:g} км від вас") if r.approaching else "курс невідомий — вважайте, що на вас"
        return f"🟠 {what}: {r.distance_km:g} км, {side}, {course}" + (f", {eta}" if eta else "") + f" ({speed})." + trust_phrase(r)
    if not r.approaching:
        return f"🟡 {what}: {r.distance_km:g} км, {side}, курс невідомий, орієнтовно — слідкуйте ({speed})." + trust_phrase(r)
    return f"🟡 {what}: {r.distance_km:g} км, {side}, може пройти за ~{r.cpa_km:g} км від вас, {eta} ({speed})." + trust_phrase(r)


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
