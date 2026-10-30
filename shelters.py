"""shelters.py — найближчі укриття до точки.

Дані — OpenStreetMap: локальний набір (data/shelters_ua.tsv.gz, без мережі), а поза ним Photon і Overpass; кеш 15 хв: amenity=shelter, building/amenity=bunker,
emergency=shelter та об'єкти з «укриття/сховище/бомбосховище» в назві. Покриття OSM неповне, тому завжди дається й запасне
посилання «шукати укриття на карті» (Google Maps / OSM) — воно працює навіть коли Overpass недоступний.
Бот НЕ гарантує, що укриття відкрите; перевіряйте на місці.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import urllib.parse

import aiohttp

logger = logging.getLogger(__name__)

PHOTON = "https://photon.komoot.io/api/"
PHOTON_QUERIES = ("укриття", "shelter")
MAX_KM = 6000
MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
UA = "AirRadarAI/1.0 (telegram bot @AirRadarAI)"
CACHE_TTL = 15 * 60
WALK_M_PER_MIN = 80
_cache: dict[tuple, tuple[float, list]] = {}


def _query(lat: float, lon: float, radius_m: int) -> str:
    a = f"(around:{radius_m},{lat:.5f},{lon:.5f})"
    return ("[out:json][timeout:20];("
            f'nwr["amenity"="shelter"]{a};nwr["building"="bunker"]{a};nwr["amenity"="bunker"]{a};nwr["emergency"="shelter"]{a};'
            f'nwr["name"~"укриття|сховище|бомбосховище",i]{a};'
            ");out center 200;")


_LOCAL: list[tuple] | None = None
LOCAL_MAX_M = 3000


def _load_local() -> list[tuple]:
    """Локальний набір укриттів (data/shelters_ua.tsv.gz, збирається build_shelters.py з OSM). Працює без мережі."""
    global _LOCAL
    if _LOCAL is None:
        import gzip
        from pathlib import Path
        rows = []
        p = Path(__file__).parent / "data" / "shelters_ua.tsv.gz"
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 5:
                        rows.append((float(parts[0]), float(parts[1]), parts[2], parts[3], parts[4]))
        _LOCAL = rows
    return _LOCAL


def local_nearest(lat: float, lon: float, limit: int = 5, max_m: int = LOCAL_MAX_M) -> list[dict]:
    out = []
    dlat, dlon = max_m / 111000, max_m / (111000 * max(0.2, math.cos(math.radians(lat))))
    for la, lo, kind, name, street in _load_local():
        if abs(la - lat) > dlat or abs(lo - lon) > dlon:
            continue
        m = round(haversine_m((lat, lon), (la, lo)))
        if m <= max_m:
            label = name or ("Бункер" if kind == "бункер" else "Укриття")
            out.append({"name": f"{label}, {street}" if street and street not in label else label, "kind": kind,
                        "lat": la, "lon": lo, "m": m, "note": ""})
    out.sort(key=lambda x: x["m"])
    return out[:limit]


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def parse_photon(features: list[dict], lat: float, lon: float, limit: int = 5) -> list[dict]:
    """Фічі Photon (geocoder поверх OSM) → найближчі укриття з адресою вулиці (це відкриті дані OSM про саме укриття)."""
    out, seen = [], set()
    for f in features:
        p = f.get("properties", {})
        c = (f.get("geometry") or {}).get("coordinates") or []
        if len(c) < 2 or p.get("osm_value") not in ("shelter", "bunker") and "укрит" not in (p.get("name", "") or "").lower() \
                and "сховищ" not in (p.get("name", "") or "").lower():
            continue
        flon, flat = c[0], c[1]
        key = (round(flat, 4), round(flon, 4))
        m = round(haversine_m((lat, lon), (flat, flon)))
        if key in seen or m > MAX_KM:
            continue
        seen.add(key)
        street = " ".join(x for x in (p.get("street"), p.get("housenumber")) if x)
        name = p.get("name") or "Укриття"
        out.append({"name": f"{name}, {street}" if street and street not in name else name,
                    "kind": "бункер" if p.get("osm_value") == "bunker" else "укриття",
                    "lat": flat, "lon": flon, "m": m, "note": ""})
    out.sort(key=lambda x: x["m"])
    return out[:limit]


def parse(elements: list[dict], lat: float, lon: float, limit: int = 5) -> list[dict]:
    """Елементи Overpass → найближчі унікальні об'єкти з відстанню (м)."""
    out, seen = [], set()
    for e in elements:
        c = e if "lat" in e else e.get("center") or {}
        if "lat" not in c:
            continue
        t = e.get("tags", {})
        if t.get("access") in ("private", "no") or t.get("disused") == "yes" or t.get("abandoned") == "yes":
            continue
        key = (round(c["lat"], 4), round(c["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        name = t.get("name") or t.get("name:uk") or ""
        kind = "бункер" if (t.get("building") == "bunker" or t.get("amenity") == "bunker") else "укриття"
        out.append({"name": name, "kind": kind, "lat": c["lat"], "lon": c["lon"], "m": round(haversine_m((lat, lon), (c["lat"], c["lon"]))),
                    "note": t.get("description") or t.get("opening_hours") or ""})
    out.sort(key=lambda x: x["m"])
    return out[:limit]


async def nearest(lat: float, lon: float, limit: int = 5, session: aiohttp.ClientSession | None = None) -> list[dict] | None:
    """Найближчі укриття; [] — не знайдено в OSM; None — сервіс карт недоступний."""
    local = local_nearest(lat, lon, limit)
    if local:
        return local
    key = (round(lat, 3), round(lon, 3))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1][:limit]
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

    async def _photon():
        """Основне джерело: Photon (швидкий, без черг). None — недоступний."""
        feats, ok = [], False
        for q in PHOTON_QUERIES:
            for attempt in range(3):
                try:
                    d = 0.07   # ≈ ±7.7 км: фільтр за областю замість ранжування за назвою
                    async with session.get(PHOTON, params={"q": q, "limit": "50", "bbox": f"{lon - d * 1.6:.5f},{lat - d:.5f},{lon + d * 1.6:.5f},{lat + d:.5f}"},
                                           headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            ok = True
                            feats += (await r.json(content_type=None)).get("features", [])
                            break
                        if r.status not in (429, 502, 503):
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                await asyncio.sleep(1.5 * (attempt + 1))
        return feats if ok else None

    async def _fetch():
        for url in MIRRORS:
            try:
                async with session.post(url, data={"data": _query(lat, lon, 3000)}, headers={"User-Agent": UA, "Accept": "application/json"},
                                        timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue
        return None

    res = None
    try:
        # Загальний бюджет ~25 с: користувач чекає відповіді в чаті, не хвилину.
        feats = await asyncio.wait_for(_photon(), timeout=20)
        if feats is not None:
            res = parse_photon(feats, lat, lon, limit)
        if not res:
            data = await asyncio.wait_for(_fetch(), timeout=16)   # запасне джерело — Overpass
            if data is not None:
                res = parse(data.get("elements", []), lat, lon, limit)
            elif feats is None:
                return None
            else:
                res = []
    except asyncio.TimeoutError:
        return None
    finally:
        if own:
            await session.close()
    _cache[key] = (time.time(), res)
    return res[:limit]


def search_links(lat: float, lon: float) -> dict[str, str]:
    q = urllib.parse.quote("укриття")
    return {
        "google": f"https://www.google.com/maps/search/{q}/@{lat:.5f},{lon:.5f},16z",
        "osm": f"https://www.openstreetmap.org/#map=16/{lat:.5f}/{lon:.5f}",
    }


def route_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=walking"


def fmt_distance(m: int) -> str:
    return f"{m} м" if m < 1000 else f"{m / 1000:.1f} км"


def format_text(results: list[dict] | None, label: str = "") -> str:
    head = "🏚 <b>Найближчі укриття</b>" + (f" — {label}" if label else "")
    if results is None:
        return head + "\n\nСервіс карт зараз недоступний, тому список не завантажився. Скористайся кнопкою «Шукати на карті» нижче — вона працює напряму."
    if not results:
        return head + ("\n\nУ відкритих даних OpenStreetMap поруч укриттів не знайдено (покриття неповне). "
                       "Скористайся пошуком на карті нижче; у великих будинках шукай позначки «Укриття» на дверях підвалу чи паркінгу.")
    lines = [head, ""]
    for i, r in enumerate(results, 1):
        walk = max(1, round(r["m"] / WALK_M_PER_MIN))
        nm = r["name"] or ("Укриття" if r["kind"] == "укриття" else "Бункер")
        lines.append(f"{i}. <b>{nm}</b> — {fmt_distance(r['m'])} (~{walk} хв пішки)")
    lines.append("\n<i>Дані OpenStreetMap, покриття неповне; бот не гарантує, що укриття відкрите. Перевіряйте на місці.</i>")
    return "\n".join(lines)
