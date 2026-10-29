#!/usr/bin/env python3
"""export_radar.py — выгрузка треков в статический tracks.json для GitHub Pages.

Карта радара на Pages читает этот файл, когда живого API нет. Сервер сам
отправляет файл наружу (исходящий git push), входящих портов не открывается.

    python export_radar.py OUT.json    # пишет файл, код 0 — данные изменились, 10 — без изменений
"""
import json
import sys
import time
from pathlib import Path

from config import load_settings
from database import Database
from health_server import _track_json
from track_engine import build_tracks

WINDOW_MIN = 360


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tracks.json")
    db = Database(load_settings().db_path)
    now = int(time.time())
    tracks = build_tracks(db.recent_observations(now - WINDOW_MIN * 60), now, window_s=WINDOW_MIN * 60)
    body = [_track_json(t, now) for t in tracks]
    payload = {"now": now, "window_min": WINDOW_MIN, "tracks": body}
    # Без активных/свежих целей и без изменений состава файл не перевыкладываем.
    changed = True
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            changed = old.get("tracks") != body or now - int(old.get("now", 0)) > 1800
        except (ValueError, OSError):
            pass
    if changed:
        out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0 if changed else 10


if __name__ == "__main__":
    sys.exit(main())
