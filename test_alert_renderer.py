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
        # Заголовок капсом — визуальный якорь.
        self.assertIn("🔴 ОДЕСЬКА ОБЛ. | БПЛА", result)
        self.assertIn("повідомлення одного джерела", result)
        self.assertIn("🕒 ", result)
        self.assertIn("БпЛА на Одесу", result)
        # Читабельность: блоки разделены пустыми строками.
        self.assertIn("\n\n", result)
        # Строка источника — всегда наш канал (пассивный брендинг).
        self.assertIn("📡 Джерело: AirRadar AI", result)
        # Хендлы исходных каналов в пост не попадают.
        self.assertNotIn("@raketa_trevoga", result)
        # Старые блоки удалены.
        self.assertNotIn("Кількість", result)
        self.assertNotIn("Не підтверджено або невідомо", result)
        # Правило 2: без ETA-прогнозов и вероятностей.
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
        # Спорное число не публикуется как факт.
        self.assertNotIn("щонайменше", result)
        self.assertNotIn("5", result)

    def test_motion_line_requires_origin_and_destination(self):
        fact = extract_incident_fact("БПЛА з Сум у напрямку Києва", "uav", "imminent")
        result = render_evidence_alert(
            text="БПЛА з Сум у напрямку Києва", source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn("Рух: цілі прямують з Сумська обл. на Київ та область", result)

        fact2 = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result2 = render_evidence_alert(
            text="БпЛА на Одесу", source="a", event_ts=1_700_000_000,
            fact=fact2, confirmation={"status": "reported", "sources": 1},
        )
        self.assertNotIn("Рух:", result2)

    def test_aggregated_sources_line(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="b", event_ts=1_700_000_000, fact=fact,
            confirmation={"status": "corroborated", "sources": 2},
            sources=["a", "b"],
        )
        # Агрегация видна в статусе; хендлы источников не публикуются.
        self.assertIn("підтверджено 2 незалежними джерелами", result)
        self.assertIn("📡 Джерело: AirRadar AI", result)
        self.assertNotIn("@a", result)
        self.assertNotIn("@b", result)

    def test_numeric_source_is_monitoring(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="-1003979438669", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn("📡 Джерело: AirRadar AI", result)
        self.assertNotIn("-1003979438669", result)

    def test_stand_down_branch(self):
        fact = extract_incident_fact("Відбій повітряної тривоги у Києві", "stand_down", "unknown")
        result = render_evidence_alert(
            text="Відбій повітряної тривоги у Києві", source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertTrue(result.startswith("🟢 ВІДБІЙ — КИЇВ ТА ОБЛАСТЬ — "))
        self.assertIn("📡 Джерело: AirRadar AI", result)


if __name__ == "__main__":
    unittest.main()
