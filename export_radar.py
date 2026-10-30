#!/usr/bin/env python3
"""export_radar.py — статичні дані для GitHub Pages: tracks.json, stats.json, impacts.json.

Mini App і радар на Pages читають ці файли, коли живого API немає. Сервер сам
відправляє їх назовні (вихідний git push), вхідних портів не відкривається.

    python export_radar.py DIR   # код 0 — щось змінилось, 10 — без змін
"""
import json
import os
import sys
import time
from pathlib import Path

from config import load_settings
from database import Database
from health_server import history_payload, radar_payload
from impact_log import DEFAULT_DELAY_S, impacts_payload
from stats_engine import stats_payload
from track_engine import build_tracks
import forecast_eval

WINDOW_MIN = 360


def _write_if_changed(path: Path, payload: dict, volatile: tuple[str, ...], max_age_s: int) -> bool:
    """Пишемо файл, якщо змінився зміст (без полів-часу) або файл застарів."""
    now = int(payload.get("now", time.time()))
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            same = all(old.get(k) == v for k, v in payload.items() if k not in volatile)
            if same and now - int(old.get("now", 0)) <= max_age_s:
                return False
        except (ValueError, OSError):
            pass
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    db = Database(load_settings().db_path)
    now = int(time.time())
    delay = int(os.getenv("IMPACT_DELAY_MIN", str(DEFAULT_DELAY_S // 60))) * 60
    # Самоперевірка прогнозів: записуємо нові, закриваємо дозрілі (на тих самих треках, що йдуть на карту).
    tracks = build_tracks(db.recent_observations(now - WINDOW_MIN * 60), now, window_s=WINDOW_MIN * 60)
    forecast_eval.log_forecasts(db, tracks, now)
    forecast_eval.evaluate(db, tracks, now)
    changed = False
    changed |= _write_if_changed(out / "tracks.json", radar_payload(db, WINDOW_MIN, now), ("now",), 1800)
    changed |= _write_if_changed(out / "alerts_hist.json", history_payload(db, now, 7), ("now",), 600)
    changed |= _write_if_changed(out / "stats.json", stats_payload(db, now), ("now",), 900)
    changed |= _write_if_changed(out / "impacts.json", impacts_payload(db, now, delay), ("now",), 900)
    return 0 if changed else 10


if __name__ == "__main__":
    sys.exit(main())
