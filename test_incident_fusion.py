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

    def test_aircraft_designation_is_not_a_count(self):
        fact = extract_incident_fact("Активність Су-34/35", "tac_aviation", "potential")
        self.assertEqual(fact.count_kind, "unspecified")
        self.assertIsNone(fact.count_value)

    def test_explicit_uav_count_is_extracted(self):
        fact = extract_incident_fact("34 БпЛА на Київ", "uav", "imminent")
        self.assertEqual(fact.count_kind, "exact")
        self.assertEqual(fact.count_value, 34)

    def test_destination_is_preferred_over_origin(self):
        fact = extract_incident_fact("БПЛА з Сум у напрямку Києва", "uav", "imminent")
        self.assertEqual(fact.origin_region, "sumska")
        self.assertEqual(fact.destination_region, "kyivska")

    def test_roundup_post_is_marked_multi_region(self):
        # Сборная сводка по нескольким областям: не присваиваем случайный регион.
        roundup = (
            "Київщина\n2 реактивні БпЛА на Васильків\n2 реактивні БпЛА на Чорнобиль\n"
            "Житомирщина\n1 реактивний БпЛА повз Овруч на Рівненщину\n"
            "Полтавщина\n1 реактивний БпЛА на Глобине\n"
            "Миколаївщина\n1 реактивний БпЛА на Вознесенськ\n"
            "Одещина\n1 реактивний БпЛА на Сергіївку"
        )
        fact = extract_incident_fact(roundup, "uav", "imminent")
        self.assertEqual(fact.destination_region, "multi")
        self.assertEqual(fact.origin_region, "")

    def test_single_destination_stays_concrete(self):
        fact = extract_incident_fact("2 реактивні БпЛА на Васильків", "uav", "imminent")
        self.assertNotEqual(fact.destination_region, "multi")

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
