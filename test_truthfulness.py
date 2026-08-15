"""Проверки жизненного цикла доказательств и географической валидации."""

import os
import tempfile
import unittest

from database import Database
from regions import validate_city_for_regions
from source_policy import normalize_source, parse_source_groups, source_group


class IncidentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_same_group_does_not_confirm_incident(self):
        first = self.db.register_incident(event_ts=1_700_000_000, weapon_class="shahed", stage="imminent", region="sumska", source="a", source_group="network", text="БПЛА")
        second = self.db.register_incident(event_ts=1_700_000_001, weapon_class="shahed", stage="imminent", region="sumska", source="b", source_group="network", text="БПЛА 2")
        self.assertEqual(first["status"], "reported")
        self.assertEqual(second["status"], "reported")
        self.assertEqual(second["sources"], 1)

    def test_independent_group_confirms_incident(self):
        self.db.register_incident(event_ts=1_700_000_000, weapon_class="shahed", stage="imminent", region="sumska", source="a", source_group="a", text="БПЛА")
        incident = self.db.register_incident(event_ts=1_700_000_001, weapon_class="shahed", stage="imminent", region="sumska", source="b", source_group="b", text="БПЛА 2")
        self.assertEqual(incident["status"], "corroborated")
        self.assertEqual(incident["sources"], 2)


class GeographyAndSourceTests(unittest.TestCase):
    def test_city_requires_source_and_matching_region(self):
        self.assertEqual(validate_city_for_regions("Суми", "БПЛА на Суми", ["sumska"]), "Суми")
        self.assertEqual(validate_city_for_regions("Львів", "БПЛА на Суми", ["sumska"]), "")
        self.assertEqual(validate_city_for_regions("Суми", "БПЛА", ["sumska"]), "")

    def test_source_groups_normalize_reposts(self):
        groups = parse_source_groups("@A=Network, @b=network")
        self.assertEqual(normalize_source("@A"), "a")
        self.assertEqual(source_group("@A", groups), "network")
        self.assertEqual(source_group("@b", groups), "network")


if __name__ == "__main__":
    unittest.main()
