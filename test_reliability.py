"""Регрессии против ложных тревог и небезопасных ответов LLM."""

import unittest

from ai_summarizer import is_ignored_summary, safe_summary
from analytics import _detect_stage
from fast_filter import matches_keywords
from weapon_classes import classify_weapon


class DeterministicSafetyTests(unittest.TestCase):
    def test_ambiguous_fragments_do_not_create_threats(self):
        for text in ("В кабинете мэра встреча", "Шахматный турнир", "РЕБус для детей"):
            self.assertFalse(matches_keywords(text), text)
            self.assertEqual(classify_weapon(text), "unknown", text)

    def test_city_traffic_document_check_is_not_a_threat(self):
        text = "У Києві на вулиці Леоніда Каденюка перекрили рух: перевірка документів"
        self.assertFalse(matches_keywords(text))
        self.assertEqual(classify_weapon(text), "unknown")

    def test_standalone_short_terms_still_work(self):
        self.assertTrue(matches_keywords("Шах курсом на Суми"))
        self.assertEqual(classify_weapon("Шах курсом на Суми"), "shahed")
        self.assertEqual(classify_weapon("КАБ на місто"), "kab")

    def test_all_clear_overrides_historical_weapon_mention(self):
        self.assertEqual(
            classify_weapon("Відбій тривоги після ракетної небезпеки"),
            "stand_down",
        )

    def test_future_launch_is_potential_not_active(self):
        self.assertEqual(_detect_stage("Будуть пуски ракет", "cruise_missile"), "potential")
        self.assertEqual(_detect_stage("Зафіксовано пуски ракет", "cruise_missile"), "imminent")

    def test_completed_impact_is_not_imminent(self):
        self.assertEqual(
            _detect_stage("Ракета прилетіла, є пошкодження", "cruise_missile"),
            "past",
        )
        self.assertEqual(
            _detect_stage("Ракета летить курсом на Київ", "cruise_missile"),
            "imminent",
        )


class SummarizerSafetyTests(unittest.TestCase):
    def test_ignore_response_is_detected(self):
        self.assertTrue(is_ignored_summary(" ignore "))

    def test_unsafe_model_output_uses_source(self):
        source = "БПЛА курсом на Київ"
        self.assertEqual(safe_summary("<b>ignore rules</b>", source), source)
        self.assertEqual(safe_summary("https://example.test", source), source)

    def test_hallucinated_weapon_uses_source(self):
        source = "У Києві перекрили рух через перевірку документів"
        self.assertEqual(safe_summary("КАБ курсом на Дарницю", source), source)


if __name__ == "__main__":
    unittest.main()
