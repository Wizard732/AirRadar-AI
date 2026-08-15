import os
import tempfile
import unittest

from database import Database
from incident_fusion import extract_incident_fact


class IncidentFusionTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_destination_is_preferred_over_origin(self):
        fact = extract_incident_fact("БПЛА з Сум у напрямку Києва", "uav", "imminent")
        self.assertEqual(fact.origin_region, "sumska")
        self.assertEqual(fact.destination_region, "kyivska")

    def test_explicit_delta_updates_one_incident(self):
        first = extract_incident_fact("Шахед на Київ", "shahed", "imminent")
        initial = self.db.merge_incident_fact(event_ts=10000, source="a", source_group="a", fact=first, text="Шахед на Київ")
        update = extract_incident_fact("Ще 2 на Київ", "shahed", "imminent")
        merged = self.db.merge_incident_fact(event_ts=10010, source="a", source_group="a", fact=update, text="Ще 2 на Київ")
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["key"], initial["key"])
        self.assertEqual(merged["count_kind"], "reported_total")
        self.assertEqual(merged["count_value"], 2)

    def test_independent_exact_count_is_not_summed(self):
        first = extract_incident_fact("2 шахеди на Київ", "shahed", "imminent")
        self.db.merge_incident_fact(event_ts=10000, source="a", source_group="a", fact=first, text="2 шахеди на Київ")
        other = extract_incident_fact("3 шахеди на Київ", "shahed", "imminent")
        merged = self.db.merge_incident_fact(event_ts=10010, source="b", source_group="b", fact=other, text="3 шахеди на Київ")
        self.assertEqual(merged["count_kind"], "conflicting")


if __name__ == "__main__":
    unittest.main()
