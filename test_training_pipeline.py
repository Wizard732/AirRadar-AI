"""Проверки локального пайплайна подготовки датасета."""

import unittest

from build_silver_dataset import make_target
from label_training_candidates import valid_label
from weapon_classes import classify_weapon
from prepare_training_dataset import target_validation_reason
from training_filter import classify_for_training


class TrainingFilterTests(unittest.TestCase):
    def test_active_shahed_is_candidate(self):
        result = classify_for_training("Реактивний шахед курсом на Суми")
        self.assertEqual(result.category, "active_threat")

    def test_impact_is_candidate(self):
        result = classify_for_training("Вибух у Сумах після атаки БпЛА")
        self.assertEqual(result.category, "impact_or_shelling")

    def test_fundraiser_is_rejected(self):
        result = classify_for_training("Терміновий збір на дрони, поповнюйте банку")
        self.assertEqual(result.category, "irrelevant")
        self.assertEqual(result.reason, "promotion_or_fundraiser")

    def test_analysis_is_rejected(self):
        result = classify_for_training("Аналітика: ворог змінює тактику застосування ракет")
        self.assertEqual(result.category, "irrelevant")
        self.assertEqual(result.reason, "analysis_or_digest")

    def test_moped_sound_is_not_a_confirmed_weapon(self):
        result = classify_for_training("У Києві чути звук, схожий на мопед")
        self.assertEqual(result.category, "irrelevant")
        self.assertEqual(result.reason, "unverified_observation")
        self.assertEqual(classify_weapon("У Києві чути звук, схожий на мопед"), "unknown")


class SilverDatasetTests(unittest.TestCase):
    def test_normalizes_reactive_shahed(self):
        self.assertEqual(
            make_target("Реактивний шахед курсом на Суми"),
            ("active_threat", "Реактивний БпЛА типу Shahed курсом на Суми."),
        )

    def test_short_shahed_name_is_normalized(self):
        self.assertEqual(
            make_target("Шах курсом на Суми"),
            ("active_threat", "БпЛА типу Shahed курсом на Суми."),
        )

    def test_banderol_is_normalized_as_shahed_family(self):
        self.assertEqual(
            make_target("Бандероль курсом на Суми"),
            ("active_threat", "БпЛА типу Shahed курсом на Суми."),
        )

    def test_rejects_uncertain_source(self):
        self.assertIsNone(make_target("Можливо ракети курсом на Київ"))

    def test_normalizes_alert(self):
        self.assertEqual(
            make_target("Повітряна тривога в Сумській області"),
            ("active_threat", "Повітряна тривога в Сумській області."),
        )


class TargetValidatorTests(unittest.TestCase):
    def test_accepts_grounded_target(self):
        label = {"keep": True, "event_type": "active_threat", "target": "Реактивний БпЛА типу Shahed курсом на Суми."}
        self.assertIsNone(target_validation_reason("Реактивний шахед курсом на Суми", label))

    def test_rejects_hallucinated_weapon(self):
        label = {"keep": True, "event_type": "active_threat", "target": "Ракета курсом на Суми."}
        self.assertEqual(target_validation_reason("Шахед курсом на Суми", label), "unsupported_weapon:ракет")

    def test_rejects_promotion_in_target(self):
        label = {"keep": True, "event_type": "active_threat", "target": "БпЛА на Суми. https://t.me/example"}
        self.assertEqual(target_validation_reason("БпЛА на Суми", label), "target_contains_promotion")

    def test_rejects_non_factual_target_text(self):
        label = {"keep": True, "event_type": "impact_or_shelling", "target": "Вибухи в Сумах. Залишайтеся в укриттях."}
        self.assertEqual(target_validation_reason("Вибухи в Сумах", label), "target_contains_non_factual_text")

    def test_label_contract_rejects_combined_event_types(self):
        label = {"keep": True, "event_type": "active_threat|impact_or_shelling", "target": "Вибухи в Сумах.", "reject_reason": ""}
        self.assertFalse(valid_label(label))


if __name__ == "__main__":
    unittest.main()
