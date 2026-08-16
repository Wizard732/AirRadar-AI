import os
import tempfile
import unittest

from bot_ui import _region_cons_text
from database import Database
from incident_fusion import extract_incident_fact


class ConfirmedUiTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_legacy_explosion_is_not_a_confirmed_consequence(self):
        self.db.add_threat("explosion", "kyivska", "КАБ курсом на Київ", event_ts=1_700_000_000)
        text = _region_cons_text(self.db, "kyivska")
        self.assertIn("Немає підтверджених", text)
        self.assertNotIn("КАБ курсом", text)

    def test_confirmed_impact_has_evidence(self):
        fact = extract_incident_fact("Влучання у Києві", "explosion", "past")
        self.db.merge_incident_fact(event_ts=1_700_000_000, source="a", source_group="a", fact=fact, text="Влучання у Києві")
        self.db.merge_incident_fact(event_ts=1_700_000_001, source="b", source_group="b", fact=fact, text="Влучання у Києві")
        text = self.db.confirmed_consequences("kyivska", since=0)
        self.assertEqual(len(text), 1)
        self.assertEqual(text[0]["source_count"], 2)


if __name__ == "__main__":
    unittest.main()
