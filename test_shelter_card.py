import asyncio
import os
import tempfile
import time
import unittest

import alarm_notices
import guard_places
import shelter_card
from database import Database
from track_engine import destination

HOME = (50.4501, 30.5234)


class Pub:
    async def send(self, text): return {"chat": {"id": "c"}, "message_id": 1}


class ShelterCardTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_card_lifecycle(self):
        now = int(time.time()); sent, edits = [], []
        async def send(uid, text, buttons): sent.append((uid, text)); return 42
        async def edit(uid, mid, text, buttons): edits.append((uid, mid, text))
        run = asyncio.run
        self.db.set_shelter_mode(5, True)
        self.db.subscribe(5, "kyivska")
        guard_places.add_place(self.db, 5, "🏠 Дім", *HOME)
        self.assertEqual(run(shelter_card.tick(self.db, send, edit, "https://x.github.io/R", now))["sent"], 0, "тривоги ще немає")
        run(alarm_notices.apply(self.db, Pub(), None, "start", ["kyivska"], now - 120, now))
        self.assertEqual(run(shelter_card.tick(self.db, send, edit, "", now))["sent"], 1)
        self.assertIn("🏠 Дім: поруч цілей зі зведень не бачу", sent[0][1])
        self.assertEqual(run(shelter_card.tick(self.db, send, edit, "", now + 5))["sent"], 0, "вдруге картку не шлемо")
        lat, lon = destination(HOME, 180.0, 6)
        for ts, d in ((now - 300, 30), (now - 150, 18), (now - 20, 6)):
            la, lo = destination(HOME, 180.0, d)
            with self.db._lock:
                self.db._conn.execute("INSERT INTO track_observations(event_ts,as_of,source,weapon,profile,count,group_count,oblast,place,relation,lat,lon,precision,heading_from)"
                                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, "", "s", "shahed", "", 1, None, "kyivska", "T", "at", la, lo, "place", ""))
                self.db._conn.commit()
        res = run(shelter_card.tick(self.db, send, edit, "", now + 60))
        self.assertEqual(res["edited"], 1)
        self.assertIn("Шахед", edits[-1][2])
        run(alarm_notices.apply(self.db, Pub(), None, "end", ["kyivska"], now + 100, now + 100))
        self.assertEqual(run(shelter_card.tick(self.db, send, edit, "", now + 110))["closed"], 1)
        self.assertIn("Тривога завершена", edits[-1][2])
        self.assertEqual(run(shelter_card.tick(self.db, send, edit, "", now + 200))["closed"], 0)

    def test_no_card_without_points(self):
        now = int(time.time()); sent = []
        async def send(uid, text, buttons): sent.append(uid); return 1
        async def edit(*a): pass
        self.db.set_shelter_mode(6, True); self.db.subscribe(6, "kyivska")
        asyncio.run(alarm_notices.apply(self.db, Pub(), None, "start", ["kyivska"], now - 10, now))
        self.assertEqual(asyncio.run(shelter_card.tick(self.db, send, edit, "", now))["sent"], 0)


if __name__ == "__main__":
    unittest.main()
