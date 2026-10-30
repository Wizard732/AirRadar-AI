"""backfill_observations.py — восстановить наблюдения для треков из журнала threat_events.

Нужен один раз после обновления (или после простоя бота): парсер разбирает тексты
последних часов заново. Идемпотентен: уже записанные (время, источник) пропускаются.
Запуск: python backfill_observations.py [часы=12]
"""

import sys
import time

from config import load_settings
from database import Database
from threat_parser import parse_post


def main(hours: float = 12) -> int:
    db = Database(load_settings().db_path)
    since = int(time.time() - hours * 3600)
    have = {(r["event_ts"], r["source"]) for r in db._conn.execute(
        "SELECT DISTINCT event_ts, source FROM track_observations WHERE event_ts >= ?", (since,))}
    seen, added = set(), 0
    for r in db._conn.execute(
            "SELECT event_ts, source, text FROM threat_events WHERE event_ts >= ? ORDER BY event_ts, id", (since,)):
        key = (r["event_ts"], r["source"], r["text"])
        if key in seen or (r["event_ts"], r["source"]) in have:
            continue
        seen.add(key)
        parsed = parse_post(r["text"], r["source"])
        if parsed.kind == "track" and parsed.observations:
            added += db.add_observations(r["event_ts"], r["source"], parsed.observations, parsed.as_of)
    print(f"добавлено наблюдений: {added}")
    return added


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 12)
