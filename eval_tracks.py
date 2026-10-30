"""eval_tracks.py — прогон track_engine по реальным постам (tests_data/): распределение
измеренных скоростей и курсов. Запуск: python eval_tracks.py [часы]"""

import collections
import statistics
import sys
from dataclasses import asdict

from eval_parser import load
from threat_parser import parse_post
from track_engine import build_tracks


def rows_from_corpus(rows, hours):
    rows = sorted(rows, key=lambda r: r["event_ts"])
    end = rows[-1]["event_ts"]
    out = []
    for r in rows:
        if r["event_ts"] < end - hours * 3600:
            continue
        p = parse_post(r["text"], r["source"])
        for o in p.observations:
            d = asdict(o)
            d.update(event_ts=r["event_ts"], as_of=p.as_of, source=r["source"])
            out.append(d)
    return out, end


if __name__ == "__main__":
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 6
    obs, end = rows_from_corpus(load(), hours)
    tracks = build_tracks(obs, end, window_s=int(hours * 3600))
    print("наблюдений", len(obs), "треков", len(tracks))
    multi = [t for t in tracks if len(t.points) >= 3]
    meas = [t.speed_kmh for t in multi if t.speed_source == "measured"]
    print("треков с ≥3 точками", len(multi), "измеренная скорость:", len(meas))
    if meas:
        print("скорость км/ч: медиана %.0f, p10 %.0f, p90 %.0f" % (
            statistics.median(meas), sorted(meas)[len(meas) // 10], sorted(meas)[len(meas) * 9 // 10]))
    print(collections.Counter(t.status for t in tracks))
    for t in sorted(multi, key=lambda t: -len(t.points))[:6]:
        print(t.id, t.weapon, t.profile or "-", len(t.points), "точек", "%.0f км/ч" % t.speed_kmh, t.speed_source,
              "курс", None if t.heading is None else round(t.heading), "→", " → ".join(p.place for p in t.points[:8]))
