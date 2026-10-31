"""map_preview.py — безпечна статична візуалізація для Telegram (без залежностей, чистий PNG).

Показує лише ОФІЦІЙНИЙ статус по областях (центроїди): червоні = тривога, сірі = немає. Жодних точних координат цілей
і прогнозів ураження — публічний продукт не слугує інструментом стеження.
"""
from __future__ import annotations

import struct
import zlib

from city_coords import REGION_CENTROIDS

W, H = 640, 400
LAT0, LAT1, LON0, LON1 = 44.0, 52.6, 22.0, 40.5
BG, GRID, OFF, ON = (24, 28, 36), (40, 46, 58), (110, 118, 130), (230, 57, 70)


def _xy(lat: float, lon: float) -> tuple[int, int]:
    return int((lon - LON0) / (LON1 - LON0) * (W - 1)), int((LAT1 - lat) / (LAT1 - LAT0) * (H - 1))


def render_png(alarm_regions: set[str]) -> bytes:
    px = [[BG] * W for _ in range(H)]
    for gx in range(0, W, 64):
        for y in range(H):
            px[y][gx] = GRID
    for gy in range(0, H, 50):
        px[gy] = [GRID] * W

    def dot(cx: int, cy: int, r: int, col) -> None:
        for y in range(max(0, cy - r), min(H, cy + r + 1)):
            for x in range(max(0, cx - r), min(W, cx + r + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    px[y][x] = col

    for slug, (lat, lon) in REGION_CENTROIDS.items():
        if slug not in alarm_regions and LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1:
            dot(*_xy(lat, lon), 5, OFF)
    for slug in alarm_regions:
        if slug in REGION_CENTROIDS:
            lat, lon = REGION_CENTROIDS[slug]
            dot(*_xy(lat, lon), 20, (110, 30, 38))
            dot(*_xy(lat, lon), 9, ON)
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
