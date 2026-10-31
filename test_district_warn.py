import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from database import Database
from district_warn import evaluate_once, plan, warning_text
from threat_parser import Observation
from track_engine import build_tracks, destination

T0 = int(datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc).timestamp())
NOW = T0 + 1200


class DistrictWarnTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.db"))
        # Курс на север по меридиану Киева из района Белой Церкви: впереди Киев.
        p0 = (49.30, 30.52)
        for i, d in enumerate((0, 25, 50)):
            lat, lon = destination(p0, 0, d)
            self.db.add_observations(T0 + i * 600, "kiev_nebo",
                                     [Observation("shahed", "", 1, None, "kyivska", f"P{i}", "at", lat, lon, "place")])
        self.sent = []

        async def send(uid, text):
            self.sent.append((uid, text))
        self.send = send

    def tearDown(self):
        self._dir.cleanup()

    def tracks(self):
        return build_tracks(self.db.recent_observations(0), NOW)

    def test_plan_warns_region_on_course_once(self):
        items = plan(self.tracks(), NOW, self.db)
        self.assertTrue(items)
        regions = [r for _, r, _ in items]
        self.assertEqual(len(regions), len(set(regions)))
        self.assertIn("kyivska", regions)

    def test_text_is_hedged_and_names_town_and_eta(self):
        t = self.tracks()[0]
        from track_engine import forecast_places
        place = forecast_places(t, NOW, horizon_min=25, min_population=15000)[0]
        text = warning_text(t, place, "kyivska")
        self.assertIn(place.name, text)
        self.assertIn("орієнтовно", text)
        self.assertIn("хв", text)

    def test_subscribers_get_it_once_then_cooldown(self):
        self.db.subscribe(11, "kyivska")
        first = asyncio.run(evaluate_once(self.db, self.send, now=NOW))
        self.assertEqual(first, 1)
        self.assertEqual(asyncio.run(evaluate_once(self.db, self.send, now=NOW + 30)), 0)
        self.assertEqual(self.sent[0][0], 11)

    def test_typical_speed_track_is_not_forecast(self):
        # одна точка → скорость типовая, курса нет: молчим
        self.db._conn.execute("DELETE FROM track_observations WHERE id > 1")
        self.db._conn.commit()
        self.assertEqual(plan(self.tracks(), NOW, self.db), [])

    def test_weapon_filter_without_uav_blocks_delivery(self):
        self.db.subscribe(12, "kyivska")
        self.db.set_weapon_classes(12, "ballistic")
        self.assertEqual(asyncio.run(evaluate_once(self.db, self.send, now=NOW)), 0)

    def test_cruise_missile_warns_even_with_typical_speed(self):
        for t in self.tracks():
            t.weapon = "cruise_missile"
            t.speed_kmh, t.speed_source = 800.0, "typical"
        items = plan(self.tracks_missile(), NOW, self.db)
        self.assertTrue(items)
        self.assertIn("Крилата ракета", items[0][2])
        self.assertRegex(items[0][2], r"\d+–\d+ хв")

    def tracks_missile(self):
        ts = self.tracks()
        for t in ts:
            t.weapon = "cruise_missile"
            t.speed_kmh, t.speed_source = 800.0, "typical"
        return ts


if __name__ == "__main__":
    unittest.main()
