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
from airradar.safety.public_view import public_impacts, regions_contract
from health_server import history_payload, public_payload
from impact_log import impacts_payload
from stats_engine import stats_payload
from track_engine import build_tracks
import forecast_eval

WINDOW_MIN = 360


def _write_if_changed(path: Path, payload: dict, volatile: tuple[str, ...], max_age_s: int) -> bool:
    """Пишемо файл, якщо змінився зміст (без полів-часу) або файл застарів."""
    now = int(payload.get("now", payload.get("updated_at", time.time())))
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            same = all(old.get(k) == v for k, v in payload.items() if k not in volatile)
            if same and now - int(old.get("now", old.get("updated_at", 0))) <= max_age_s:
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
    # Самоперевірка прогнозів: записуємо нові, закриваємо дозрілі (на тих самих треках, що йдуть на карту).
    tracks = build_tracks(db.recent_observations(now - WINDOW_MIN * 60), now, window_s=WINDOW_MIN * 60)
    forecast_eval.log_forecasts(db, tracks, now)
    forecast_eval.evaluate(db, tracks, now)
    changed = False
    # Формат — PUBLIC_MODE (за замовчуванням повні дані; region — узагальнено), затримка — TRACKS_PUBLIC_DELAY_MIN.
    payload = public_payload(db, WINDOW_MIN, now)
    changed |= _write_if_changed(out / "tracks.json", payload, ("now", "updated_at"), 1800)
    if payload.get("public"):
        changed |= _write_if_changed(out / "public-regions.json", regions_contract(payload), ("now", "updated_at"), 1800)
    changed |= _write_if_changed(out / "alerts_hist.json", history_payload(db, now, 7), ("now",), 600)
    changed |= _write_if_changed(out / "stats.json", stats_payload(db, now), ("now",), 900)
    # Наслідки: як раніше (затримка IMPACT_DELAY_MIN); у режимі PUBLIC_MODE=region — агреговано за областю (docs/impacts-audit.md).
    if payload.get("public"):
        imp = public_impacts(impacts_payload(db, now, 0))
    else:
        imp = impacts_payload(db, now, int(os.getenv("IMPACT_DELAY_MIN", "0")) * 60)
    changed |= _write_if_changed(out / "impacts.json", imp, ("now",), 900)
    return 0 if changed else 10


if __name__ == "__main__":
    sys.exit(main())
