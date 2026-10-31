"""map_preview.py — безпечна статична візуалізація для Telegram (без залежностей, чистий PNG).

Показує лише ОФІЦІЙНИЙ статус по областях (центроїди): червоні = тривога, сірі = немає. Жодних точних координат цілей
і прогнозів ураження — публічний продукт не слугує інструментом стеження.
"""
from __future__ import annotations

import struct
import zlib

import json
import math
from pathlib import Path

from city_coords import REGION_CENTROIDS

W, H = 960, 640
LAT0, LAT1, LON0, LON1 = 44.0, 52.6, 22.0, 40.5
_K = math.cos(math.radians(48.3))          # стиснення довготи, щоб карта не була «розтягнута»
SEA, LAND, LAND_ON = (18, 24, 36), (62, 72, 86), (150, 44, 54)
BORDER = (34, 40, 52)
CITY, ON_DOT = (220, 226, 236), (255, 90, 100)
# slug області → назви в miniapp/raions.json (поле "o", перше слово)
_OBL = {
    "cherkaska": ("Черкаська",), "chernigivska": ("Чернігівська",), "chernivetska": ("Чернівецька",), "crimea": ("Автономна", "Севастополь"),
    "dnipropetrovska": ("Дніпропетровська",), "donetska": ("Донецька",), "ivano_frankivska": ("Івано-Франківська",),
    "kharkivska": ("Харківська",), "khersonska": ("Херсонська",), "khmelnytska": ("Хмельницька",), "kirovohradska": ("Кіровоградська",),
    "kyivska": ("Київська", "Київ"), "kyiv_city": ("Київ",), "luhanska": ("Луганська",), "lvivska": ("Львівська",),
    "mykolaivska": ("Миколаївська",), "odeska": ("Одеська",), "poltavska": ("Полтавська",), "rivnenska": ("Рівненська",),
    "sumska": ("Сумська",), "ternopilska": ("Тернопільська",), "vinnytska": ("Вінницька",), "volynska": ("Волинська",),
    "zakarpatska": ("Закарпатська",), "zaporizka": ("Запорізька",), "zhytomyrska": ("Житомирська",),
}
_CITIES = [(50.45, 30.52), (49.99, 36.23), (46.48, 30.73), (48.46, 35.04), (49.84, 24.03), (47.84, 35.14), (46.97, 32.0),
           (50.62, 26.25), (49.23, 28.47), (48.29, 25.94), (51.5, 31.3), (50.91, 34.8), (49.59, 34.55), (48.52, 34.62)]
_GEO: list | None = None


def _xy(lat: float, lon: float) -> tuple[float, float]:
    return (lon - LON0) / (LON1 - LON0) * (W - 1), (LAT1 - lat) / (LAT1 - LAT0) * (H - 1)


def _geo() -> list:
    """Райони з raions.json у піксельних координатах: [(область, [кільця])]."""
    global _GEO
    if _GEO is None:
        g = json.loads((Path(__file__).resolve().parent.parent.parent / "miniapp" / "raions.json").read_text(encoding="utf-8"))
        out = []
        for f in g["features"]:
            geom = f["geometry"]
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            rings = [[_xy(pt[1], pt[0]) for pt in ring] for poly in polys for ring in poly]
            out.append((f["properties"]["o"].split(" ")[0], rings))
        _GEO = out
    return _GEO


def _fill(px, rings, col) -> None:
    """Заливка багатокутника (парно-непарне правило: дірки працюють)."""
    ys = [p[1] for r in rings for p in r]
    for y in range(max(0, int(min(ys))), min(H, int(max(ys)) + 1)):
        xs = []
        for r in rings:
            j = len(r) - 1
            for i in range(len(r)):
                (x1, y1), (x2, y2) = r[j], r[i]
                if (y1 > y) != (y2 > y):
                    xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
                j = i
        xs.sort()
        row = px[y]
        for k in range(0, len(xs) - 1, 2):
            for x in range(max(0, int(xs[k])), min(W, int(xs[k + 1]) + 1)):
                row[x] = col


def _line(px, a, b, col) -> None:
    n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) + 1
    for i in range(n + 1):
        x, y = int(a[0] + (b[0] - a[0]) * i / n), int(a[1] + (b[1] - a[1]) * i / n)
        if 0 <= x < W and 0 <= y < H:
            px[y][x] = col


def render_png(alarm_regions: set[str]) -> bytes:
    """Карта України з реальними межами областей/районів. Лише офіційний статус по областях (заливка), без точок цілей."""
    on = {n for slug in alarm_regions for n in _OBL.get(slug, ())}
    px = [[SEA] * W for _ in range(H)]
    geo = _geo()
    for obl, rings in geo:                       # суша
        _fill(px, rings, LAND_ON if obl in on else LAND)
    for obl, rings in geo:                       # межі районів
        for r in rings:
            for a, b in zip(r, r[1:]):
                _line(px, a, b, BORDER if obl not in on else (112, 34, 44))
    for lat, lon in _CITIES:
        x, y = _xy(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if 0 <= int(x) + dx < W and 0 <= int(y) + dy < H:
                    px[int(y) + dy][int(x) + dx] = CITY
    for slug in alarm_regions:                   # маркер тривоги в центроїді області
        if slug in REGION_CENTROIDS:
            cx, cy = map(int, _xy(*REGION_CENTROIDS[slug]))
            for y in range(max(0, cy - 7), min(H, cy + 8)):
                for x in range(max(0, cx - 7), min(W, cx + 8)):
                    if (x - cx) ** 2 + (y - cy) ** 2 <= 49:
                        px[y][x] = ON_DOT
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def caption(alarm_regions: set[str], updated: str) -> str:
    from regions import region_name
    from safety_policy import DISCLAIMER
    head = ("🚨 Офіційна тривога: " + ", ".join(sorted(region_name(r) for r in alarm_regions))) if alarm_regions \
        else "🟢 Офіційних тривог за даними сирен немає"
    return f"{head}\nОновлено: {updated}\n\n{DISCLAIMER}"
