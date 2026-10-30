"""build_events: КАБ/ракеты/авиация/FPV попадают на карту как одиночные события."""
import time
import unittest

from track_engine import build_events


def row(weapon, ts, lat=49.99, lon=36.23, precision="place", relation="toward", count=None):
    return {"event_ts": ts, "as_of": "", "source": "x", "weapon": weapon, "profile": "", "count": count,
            "oblast": "kharkivska", "place": "Харків", "relation": relation, "lat": lat, "lon": lon,
            "precision": precision}


class EventsTests(unittest.TestCase):
    def test_non_tracked_weapons_become_events(self):
        now = int(time.time())
        rows = [row("kab", now - 60, count=2), row("kab", now - 30, count=3), row("fpv", now - 100, relation="at"),
                row("shahed", now - 60), row("kab", now - 7200), row("kab", now - 10, lat=None, precision="none"),
                row("ballistic", now - 20, relation="from")]
        ev = build_events(rows, now)
        self.assertEqual(sorted(e["weapon"] for e in ev), ["fpv", "kab"])
        kab = next(e for e in ev if e["weapon"] == "kab")
        self.assertEqual(kab["count"], 3)       # дубликаты схлопнуты
        self.assertTrue(kab["toward"])


if __name__ == "__main__":
    unittest.main()
