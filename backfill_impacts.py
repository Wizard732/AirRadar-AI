#!/usr/bin/env python3
"""backfill_impacts.py — наповнити журнал наслідків з уже збережених постів (за N годин, типово 48). Нічого не публікує."""
import sys

from config import load_settings
from database import Database
import impact_log
from threat_parser import parse_post


def main() -> int:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 48
    db = Database(load_settings().db_path)
    import time
    since = int(time.time()) - hours * 3600
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT event_ts, source, text FROM threat_events WHERE event_ts>=? ORDER BY event_ts", (since,)).fetchall()  # noqa: SLF001
    seen, added = set(), 0
    for ts, src, text in rows:
        key = (src, ts, text)
        if key in seen:
            continue
        seen.add(key)
        if impact_log.record_from_post(db, text, src or "", ts, parse_post(text, src or "")):
            added += 1
    print(f"posts={len(rows)} impacts_added={added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
