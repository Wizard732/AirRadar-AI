import os
import tempfile
import time
import unittest

import forecast_eval
from database import Database
from track_engine import Point, Track


def mk(points, key="uav:1:50.00:30.00"):
    t = Track(id="t1", weapon="uav", profile="")
    t.points = [Point(ts, lat, lon, 1, "x", "place") for ts, lat, lon in points]
    t.heading, t.speed_kmh, t.speed_source, t.status = 0.0, 180.0, "measured", "active"
    return t


class ForecastEvalTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _row(self, town_lat, town_lon, made, eta, key):
        forecast_eval.ensure(self.db)
        with self.db._lock:
            self.db._conn.execute("INSERT INTO forecast_log(track_key,town,lat,lon,made_ts,eta_ts) VALUES (?,?,?,?,?,?)",
                                  (key, "Місто", town_lat, town_lon, made, eta))
            self.db._conn.commit()

    def test_hit_miss_unknown_and_accuracy(self):
        now = int(time.time())
        made, eta = now - 4000, now - 3000
        hit = mk([(made, 50.0, 30.0), (eta - 60, 50.5, 30.0)]); hit_key = hit.key
        miss = mk([(made, 50.0, 31.0), (made + 600, 50.3, 31.0), (eta, 50.6, 31.0)]); 
        miss.points[0].ts = made + 1  # другий ключ
        miss_key = miss.key
        self._row(50.5, 30.0, made, eta, hit_key)       # прошёл рядом -> hit
        self._row(50.6, 30.6, made, eta, miss_key)      # трек дошёл, но мимо (≈40 км) -> miss
        self._row(48.0, 30.0, made, eta, "gone:0:0:0")  # трек исчез -> unknown
        n = forecast_eval.evaluate(self.db, [hit, miss], now)
        self.assertEqual(n, 3)
        acc = forecast_eval.accuracy(self.db, now)
        self.assertEqual((acc["hits"], acc["misses"], acc["unknown"]), (1, 1, 1))
        self.assertIsNone(acc["pct"], "мало разрешённых прогнозов — процент не показываем")


if __name__ == "__main__":
    unittest.main()
