"""Сквозные проверки: threat_parser внутри _process_message и API карты."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from test_impact_journal import FakePublisher, FakeSummarizer, _event, _import_main

from database import Database
from dedup import DedupCache

ROUNDUP = """⚠️ → Рух шахедів станом на 21:50:

• 2 на Сумщині — Боромля (1), Лебедин (1).

• 9 на Полтавщині — Гадяч (1), Хорол (1), Полтава (1), Кременчук(1).

• 8 на Дніпропетровщині — Лихівка (2), Царичанка (1), Синельникове (1)."""

NEWS = "⚡️Росія запланувала виготовити 30 тисяч «Шахедів», — ГУР\n\nРосія запланувала у 2025 році виробити не менше 70 тисяч дронів, повідомив заступник голови розвідки."
FOREIGN = '🔥Московську область масовано атакували дрони: під ударом ЛВДС "Володарська"'


class PipelineParserTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        self.main = _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def process(self, text, ts=None):
        ts = ts or datetime.now(timezone.utc)
        pub = FakePublisher()
        asyncio.run(self.main._process_message(_event(text, ts), FakeSummarizer(), pub, DedupCache(ttl=60), self.db))
        return pub

    def regions(self):
        return {r["region"] for r in self.db._conn.execute("SELECT region FROM threats").fetchall()}

    def test_roundup_writes_every_oblast_and_observations(self):
        self.process(ROUNDUP)
        self.assertTrue({"sumska", "poltavska", "dnipropetrovska"} <= self.regions())
        rows = self.db.recent_observations(0)
        self.assertGreaterEqual(len(rows), 9)
        self.assertTrue(all(r["weapon"] == "shahed" for r in rows))
        self.assertEqual({r["as_of"] for r in rows}, {"21:50"})

    def test_news_and_foreign_never_reach_the_map(self):
        self.process(NEWS)
        self.process(FOREIGN)
        self.assertEqual(self.regions(), set())
        self.assertEqual(self.db.recent_observations(0), [])
        audit = [r["reason"] for r in self.db._conn.execute("SELECT reason FROM inference_audit").fetchall()]
        self.assertEqual(sorted(audit), ["foreign", "news"])

    def test_short_form_without_classifier_keywords_is_rescued(self):
        self.process("Чернігів, шахед на місто!")
        rows = self.db.recent_observations(0)
        self.assertEqual([r["place"] for r in rows], ["Чернігів"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        import health_server
        self.hs = health_server
        health_server.set_db(self.db)

    def tearDown(self):
        self.hs.set_db(None)
        self.db.close()
        os.unlink(self.file.name)

    def get(self, handler, query):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/x?" + query)
        resp = asyncio.run(handler(req))
        return resp.status, json.loads(resp.text)

    def seed(self):
        from threat_parser import Observation
        from track_engine import destination
        now = int(time.time())
        p0 = (49.0, 31.0)
        for i, d in enumerate((0, 30, 60)):
            lat, lon = destination(p0, 0, d)
            self.db.add_observations(now - (2 - i) * 600, "kiev_nebo",
                                     [Observation("shahed", "", 2, None, "kyivska", f"P{i}", "at", lat, lon, "place")])
        return destination(p0, 0, 68)

    def test_tracks_endpoint_returns_motion_and_forecast_fields(self):
        self.seed()
        status, body = self.get(self.hs._api_tracks, "minutes=60")
        self.assertEqual(status, 200)
        t = body["tracks"][0]
        self.assertEqual(t["weapon"], "shahed")
        self.assertEqual(len(t["points"]), 3)
        self.assertAlmostEqual(t["speed_kmh"], 180, delta=15)
        self.assertEqual(t["heading_text"], "північ")
        self.assertIn("forecast", t)

    def test_risk_endpoint_warns_point_on_course(self):
        user = self.seed()
        status, body = self.get(self.hs._api_risk, f"lat={user[0]}&lon={user[1]}&radius=10")
        self.assertEqual(status, 200)
        self.assertEqual(body["risks"][0]["level"], "imminent")
        self.assertIn("км", body["risks"][0]["text"])

    def test_risk_requires_coordinates(self):
        status, _ = self.get(self.hs._api_risk, "radius=10")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
