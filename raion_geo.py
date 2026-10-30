"""raion_geo.py — точка → район України (за miniapp/raions.json). Спільно для сторожа, ризиків і карти."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_RAIONS: list | None = None


def _raions() -> list:
    global _RAIONS
    if _RAIONS is None:
        import json
        from pathlib import Path
        _RAIONS = []
        try:
            data = json.loads((Path(__file__).with_name("miniapp") / "raions.json").read_text(encoding="utf-8"))
            for f in data.get("features", []):
                g = f["geometry"]
                polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
                xs = [pt[0] for p in polys for pt in p[0]]
                ys = [pt[1] for p in polys for pt in p[0]]
                key = f["properties"]["r"] + "|" + f["properties"]["o"].split(" ")[0]
                _RAIONS.append((key, (min(xs), min(ys), max(xs), max(ys)), polys))
        except (OSError, ValueError, KeyError):
            logger.warning("raions.json недоступний — тривоги за районом вимкнено")
    return _RAIONS


def _in_ring(ring: list, x: float, y: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        if (ring[i][1] > y) != (ring[j][1] > y) and x < (ring[j][0] - ring[i][0]) * (y - ring[i][1]) / (ring[j][1] - ring[i][1]) + ring[i][0]:
            inside = not inside
        j = i
    return inside


_RAION_CACHE: dict[tuple[float, float], tuple[str, str] | None] = {}


def raion_at(lat: float, lon: float) -> tuple[str, str] | None:
    """(ключ «Район|Область», назва району) для точки; None — поза районами."""
    ck = (round(lat, 3), round(lon, 3))
    if ck in _RAION_CACHE:
        return _RAION_CACHE[ck]
    found = None
    for key, (x0, y0, x1, y1), polys in _raions():
        if x0 <= lon <= x1 and y0 <= lat <= y1 and any(_in_ring(p[0], lon, lat) for p in polys):
            found = (key, key.split("|")[0])
            break
    if len(_RAION_CACHE) > 5000:
        _RAION_CACHE.clear()
    _RAION_CACHE[ck] = found
    return found
