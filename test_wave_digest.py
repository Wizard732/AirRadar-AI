import asyncio
import os
import tempfile
import time
import unittest

from database import Database
import wave_digest


class FakePub:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, text):
        self.sent.append(text)
        return {"chat": {"id": "c"}, "message_id": 7}

    async def edit(self, chat_id, message_id, text):
        self.edits.append(text)
        return True


def obs(db, ts, weapon, place, lat, lon, count=1, rel="at"):
    with db._lock:
        db._conn.execute("INSERT INTO track_observations(event_ts,as_of,source,weapon,profile,count,group_count,oblast,place,relation,lat,lon,precision,heading_from)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, "", "s", weapon, "", count, None, "kharkivska", place, rel, lat, lon, "place", ""))
        db._conn.commit()


class WaveDigestTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_lifecycle_start_edit_close(self):
        now = int(time.time()); pub = FakePub()
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, now)), "idle")
        for i, (lat, lon) in enumerate([(49.9, 36.2), (50.9, 34.8), (50.4, 30.5), (48.5, 35.0)]):
            obs(self.db, now - 120, "shahed", f"П{i}", lat, lon)
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, now)), "started")
        self.assertIn("ХВИЛЯ", pub.sent[0]); self.assertIn("БпЛА", pub.sent[0])
        # новая цель → текст изменился, но раньше MIN_EDIT_S не редактируем
        obs(self.db, now - 60, "shahed", "Нова", 49.0, 33.0)
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, now + 30)), "idle")
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, now + wave_digest.MIN_EDIT_S + 10)), "edited")
        # тишина → закрытие итогом
        later = now + 3 * 3600
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, later)), "closed")
        self.assertIn("ЗАВЕРШЕНА", pub.edits[-1]); self.assertIn("не офіційний відбій", pub.edits[-1])
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, later + 10)), "idle")

    def test_small_activity_does_not_open_digest(self):
        now = int(time.time()); pub = FakePub()
        obs(self.db, now - 60, "shahed", "А", 49.9, 36.2); obs(self.db, now - 60, "shahed", "Б", 50.9, 34.8)
        self.assertEqual(asyncio.run(wave_digest.tick(self.db, pub, now)), "idle")
        self.assertFalse(pub.sent)


if __name__ == "__main__":
    unittest.main()
