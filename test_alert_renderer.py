import unittest

from alert_renderer import render_evidence_alert
from incident_fusion import extract_incident_fact


class EvidenceAlertTests(unittest.TestCase):
    def test_single_source_alert_only_contains_evidence(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="raketa_trevoga", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1, "count_kind": "unspecified"},
        )
        self.assertIn("повідомлено одним джерелом", result)
        self.assertIn("Кількість: не зазначено", result)
        self.assertIn("Джерело: @raketa_trevoga", result)
        self.assertNotIn("ETA", result)
        self.assertNotIn("Аналіз", result)
        self.assertNotIn("Ризик", result)
        self.assertNotIn("укритті", result)

    def test_conflicting_count_is_not_aggregated(self):
        fact = extract_incident_fact("3 БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="3 БпЛА на Одесу", source="source", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "corroborated", "sources": 2, "count_kind": "conflicting", "count_value": 2},
        )
        self.assertIn("підтверджено 2 незалежними джерелами", result)
        self.assertIn("точне число невідоме", result)
        self.assertNotIn("5", result)


if __name__ == "__main__":
    unittest.main()
