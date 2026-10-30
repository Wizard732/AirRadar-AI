import asyncio
import os
import tempfile
import time
import unittest

import guard_places
import proximity
from database import Database
from track_engine import destination

HOME = (50.4501, 30.5234)
WORK = (49.9935, 36.2304)   # Харків — далеко от дома


def obs(db, ts, dist_km, point):
    lat, lon = destination(point, 180.0, dist_km)
    with db._lock:
        db._conn.execute("INSERT INTO track_observations(event_ts,as_of,source,weapon,profile,count,group_count,oblast,place,relation,lat,lon,precision,heading_from)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, "", "s", "shahed", "", 1, None, "x", "T", "at", lat, lon, "place", ""))
        db._conn.commit()


class PlacesTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_limit_update_remove(self):
        for i in range(5):
            self.assertTrue(guard_places.add_place(self.db, 1, f"П{i}", 50, 30)[0])
        self.assertFalse(guard_places.add_place(self.db, 1, "Шоста", 50, 30)[0])
        self.assertTrue(guard_places.add_place(self.db, 1, "П0", 51, 31)[0], "те саме ім'я — оновлення")
        self.assertEqual(len(guard_places.list_places(self.db, 1)), 5)
        pid = guard_places.list_places(self.db, 1)[0]["id"]
        self.assertTrue(guard_places.remove_place(self.db, 1, pid))
        self.assertEqual(len(guard_places.list_places(self.db, 1)), 4)
        self.assertEqual(len(guard_places.list_places(self.db, 2)), 0)

    def test_guard_checks_every_place_and_names_the_one_in_danger(self):
        now = int(time.time())
        guard_places.add_place(self.db, 1, "🏠 Дім", *HOME)
        guard_places.add_place(self.db, 1, "💼 Робота", *WORK)
        for ts, d in ((now - 600, 32), (now - 330, 20), (now - 60, 8.5)):
            obs(self.db, ts, d, HOME)
        sent = []

        async def send(uid, text, **kw):
            sent.append((uid, text))

        n = asyncio.run(proximity.evaluate_once(self.db, send, now, "https://x.github.io/R"))
        self.assertEqual(n, 1, "тривога лише по дому")
        self.assertIn("🏠 Дім", sent[0][1])
        self.assertNotIn("Робота", sent[0][1])
        # повтор того ж рівня не шле вдруге
        self.assertEqual(asyncio.run(proximity.evaluate_once(self.db, send, now + 20, "")), 0)

    def test_removed_place_stops_alerts(self):
        now = int(time.time())
        guard_places.add_place(self.db, 1, "Дім", *HOME)
        for ts, d in ((now - 600, 32), (now - 330, 20), (now - 60, 8.5)):
            obs(self.db, ts, d, HOME)
        guard_places.remove_place(self.db, 1, guard_places.list_places(self.db, 1)[0]["id"])
        async def send(uid, text, **kw): raise AssertionError("не повинно слати")
        self.assertEqual(asyncio.run(proximity.evaluate_once(self.db, send, now, "")), 0)


if __name__ == "__main__":
    unittest.main()


class PersistentTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_persistent_repeats_are_capped_and_spaced(self):
        now = int(time.time())
        guard_places.add_place(self.db, 1, "Дім", *HOME)
        for ts, d in ((now - 600, 32), (now - 330, 20), (now - 60, 8.5)):
            obs(self.db, ts, d, HOME)
        log = []
        cur = [now]
        async def send(uid, text, **kw): log.append((cur[0], text))
        def run(t):
            cur[0] = t
            return asyncio.run(proximity.evaluate_once(self.db, send, t, ""))
        run(now)
        self.assertFalse(any("ПОВТОР" in t for _, t in log))
        for t in range(now + 5, now + 120, 5):            # без режиму повторів немає
            run(t)
        self.assertFalse(any("ПОВТОР" in t for _, t in log), "без наполегливого режиму повторів немає")
        self.assertTrue(guard_places.toggle_persistent(self.db, 1))
        for t in range(now + 125, now + 400, 5):
            run(t)
        reps = [ts for ts, t in log if "ПОВТОР" in t]
        self.assertEqual(len(reps), 2, "максимум 2 повтори")
        self.assertGreaterEqual(reps[1] - reps[0], 45)
        self.assertFalse(guard_places.toggle_persistent(self.db, 1))
