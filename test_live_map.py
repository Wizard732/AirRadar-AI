import asyncio
import tempfile
import unittest

from database import Database
from health_server import _api_threats, set_db
from incident_fusion import extract_incident_fact


class LiveMapApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(delete=False)
        self.temp.close()
        self.db = Database(self.temp.name)
        fact = extract_incident_fact("2 БПЛА на Київ", "uav", "imminent")
        self.db.merge_incident_fact(event_ts=__import__("time").time_ns() // 1_000_000_000,
                                    source="one", source_group="one", fact=fact, text="2 БПЛА на Київ")
        self.db.merge_incident_fact(event_ts=__import__("time").time_ns() // 1_000_000_000,
                                    source="two", source_group="two", fact=fact, text="2 БПЛА на Київ")
        set_db(self.db)

    def tearDown(self):
        self.db.close()
        __import__("os").unlink(self.temp.name)

    def test_map_payload_has_confirmation_data(self):
        response = asyncio.run(_api_threats(None))
        data = __import__("json").loads(response.text)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["threats"][0]["sources"], 2)
        self.assertEqual(data["threats"][0]["status"], "corroborated")

    def test_map_includes_single_source_reported_events(self):
        """Карта не должна быть пустой, пока источник один: reported отдаётся
        полупрозрачным («очікує підтвердження») — одобренный UI карты."""
        fact = extract_incident_fact("БпЛА на Сумщині", "uav", "imminent")
        self.db.merge_incident_fact(event_ts=__import__("time").time_ns() // 1_000_000_000,
                                    source="solo", source_group="solo", fact=fact,
                                    text="БпЛА на Сумщині")
        response = asyncio.run(_api_threats(None))
        data = __import__("json").loads(response.text)
        types = {(t["region"], t["status"], t["sources"]) for t in data["threats"]}
        self.assertIn(("sumska", "reported", 1), types)
        # Подтверждённое киевское событие из setUp тоже на месте.
        self.assertEqual(data["count"], 2)


if __name__ == "__main__":
    unittest.main()
