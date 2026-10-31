"""geo_quality.py — перевірки якості географічних даних: координати, дублікати, належність до України/області.

Нічого не змінює: повертає звіти. Очищена копія створюється окремо (scripts/check_geo_data.py --write-clean),
оригінальні файли не перезаписуються.
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
UA_BBOX = (44.0, 22.0, 52.6, 40.4)   # lat_min, lon_min, lat_max, lon_max
OBLAST_NAMES = {   # slug газетира → назва області в raions.json (поле "o", перше слово)
    "cherkaska": "Черкаська", "chernigivska": "Чернігівська", "chernivetska": "Чернівецька", "crimea": "Автономна",
    "dnipropetrovska": "Дніпропетровська", "donetska": "Донецька", "ivano_frankivska": "Івано-Франківська",
    "kharkivska": "Харківська", "khersonska": "Херсонська", "khmelnytska": "Хмельницька", "kirovohradska": "Кіровоградська",
    "kyivska": "Київська", "luhanska": "Луганська", "lvivska": "Львівська", "mykolaivska": "Миколаївська", "odeska": "Одеська",
    "poltavska": "Полтавська", "rivnenska": "Рівненська", "sumska": "Сумська", "ternopilska": "Тернопільська",
    "vinnytska": "Вінницька", "volynska": "Волинська", "zakarpatska": "Закарпатська", "zaporizka": "Запорізька",
    "zhytomyrska": "Житомирська",
}


def in_bbox(lat: float, lon: float) -> bool:
    return UA_BBOX[0] <= lat <= UA_BBOX[2] and UA_BBOX[1] <= lon <= UA_BBOX[3] and not (math.isnan(lat) or math.isnan(lon))


def _ring_contains(ring: list, lon: float, lat: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi:
            inside = not inside
        j = i
    return inside


class Boundaries:
    """Полігони районів з miniapp/raions.json з bbox-префільтром."""

    def __init__(self, path: Path | None = None):
        g = json.loads((path or ROOT / "miniapp" / "raions.json").read_text(encoding="utf-8"))
        self.items = []
        for f in g["features"]:
            geom = f["geometry"]
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            xs = [p[0] for poly in polys for p in poly[0]]
            ys = [p[1] for poly in polys for p in poly[0]]
            self.items.append((min(xs), min(ys), max(xs), max(ys), polys, f["properties"]["o"].split(" ")[0], f["properties"]["r"]))

    def locate(self, lat: float, lon: float) -> tuple[str, str] | None:
        """(область, район) або None, якщо точка поза всіма районами України."""
        for x0, y0, x1, y1, polys, obl, rai in self.items:
            if x0 <= lon <= x1 and y0 <= lat <= y1:
                for poly in polys:
                    if _ring_contains(poly[0], lon, lat) and not any(_ring_contains(h, lon, lat) for h in poly[1:]):
                        return obl, rai
        return None


def _near_boundary(self: "Boundaries", lat: float, lon: float, tol: float = 0.02) -> bool:
    """Точка в межах tol° (~2 км) від вершини контуру району: похибка спрощених полігонів на кордоні, не «чужа країна»."""
    for x0, y0, x1, y1, polys, _o, _r in self.items:
        if x0 - tol <= lon <= x1 + tol and y0 - tol <= lat <= y1 + tol:
            for poly in polys:
                for px, py, *_ in poly[0]:
                    if abs(px - lon) <= tol and abs(py - lat) <= tol:
                        return True
    return False


Boundaries.near_boundary = _near_boundary


def _rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            yield ln, line.rstrip("\n").split("\t")


def check_gazetteer(path: Path | None = None, bounds: Boundaries | None = None, sample_membership: int = 0) -> dict:
    """Координати, дублікати (ім'я+область в межах ~2 км), омоніми між областями, (опц.) належність до області."""
    path = path or ROOT / "data" / "gazetteer_ua.tsv.gz"
    bad_fmt, bad_coord, dups, seen, names = 0, 0, 0, {}, {}
    rows = []
    for ln, p in _rows(path):
        if len(p) != 5:
            bad_fmt += 1; continue
        try:
            lat, lon = float(p[2]), float(p[3])
        except ValueError:
            bad_fmt += 1; continue
        if not in_bbox(lat, lon):
            bad_coord += 1; continue
        key = (p[0].lower(), p[1])
        near = seen.get(key)
        if near and abs(near[0] - lat) < 0.02 and abs(near[1] - lon) < 0.03:
            dups += 1
        else:
            seen[key] = (lat, lon)
        names.setdefault(p[0].lower(), set()).add(p[1])
        rows.append((p[0], p[1], lat, lon))
    out = {"rows": len(rows), "bad_format": bad_fmt, "out_of_bbox": bad_coord, "duplicates_near": dups,
           "homonyms_multi_oblast": sum(1 for v in names.values() if len(v) > 1)}
    if sample_membership and bounds:
        step = max(1, len(rows) // sample_membership)
        checked = mismatched = outside = 0
        for name, obl, lat, lon in rows[::step]:
            checked += 1
            hit = bounds.locate(lat, lon)
            if hit is None:
                outside += 1
            elif OBLAST_NAMES.get(obl) and hit[0] != OBLAST_NAMES[obl] and hit[0] not in ("Київ", "Севастополь"):
                mismatched += 1
        out.update({"membership_checked": checked, "membership_mismatch": mismatched, "membership_outside_ua": outside})
    return out


def check_shelters(path: Path | None = None, bounds: Boundaries | None = None) -> dict:
    """Формат, координати, дублікати (~10 м), точки поза Україною (наприклад, сусідні країни в bbox)."""
    path = path or ROOT / "data" / "shelters_ua.tsv.gz"
    bad, outside, dups, seen, good = 0, 0, 0, set(), []
    for ln, p in _rows(path):
        if len(p) < 5:
            bad += 1; continue
        try:
            lat, lon = float(p[0]), float(p[1])
        except ValueError:
            bad += 1; continue
        if not in_bbox(lat, lon) or (bounds and bounds.locate(lat, lon) is None and not bounds.near_boundary(lat, lon)):
            outside += 1; continue
        k = (round(lat, 4), round(lon, 4), p[3].lower())
        if k in seen:
            dups += 1; continue
        seen.add(k)
        good.append(p)
    return {"rows": len(good) + bad + outside + dups, "valid": len(good), "bad_format": bad, "outside_ukraine": outside, "duplicates": dups,
            "_good": good}


def write_clean_shelters(good: list, dst: Path) -> int:
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        for p in good:
            f.write("\t".join(p) + "\n")
    return len(good)
