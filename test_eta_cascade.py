"""Тесты каскада ETA: история → события → риск → справка.

Фон: мониторные ТГК публикуют пуски стабильно, а прилёты — редко, поэтому
ETA-кнопка не должна зависеть только от пар пуск→прилёт в threats.
"""

import os
import tempfile
import time
import unittest

from database import Database
from eta import build_eta_text, estimate_event_eta, estimate_event_risk


def _add_event(db: Database, ts: int, weapon: str, stage: str, region: str = "kyivska") -> None:
    db.add_event(
        event_ts=ts, weapon_class=weapon, stage=stage, region=region,
        text=f"{weapon} {stage}", source="test", outcome="unknown", confidence=0.8,
    )


# База внутри окна выборки HISTORY_DAYS (30 дней): сейчас минус 5 суток.
BASE = int(time.time()) - 5 * 86400


class EtaCascadeTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_empty_db_shows_reference_sheet(self):
        text = build_eta_text(self.db, "kyivska", "Київська область")
        self.assertIn("Довідково", text)
        self.assertIn("орієнтовно", text.lower())
        # Справка по классам присутствует.
        self.assertIn("БПЛА", text)
        self.assertIn("Балістика", text)

    def test_event_pairs_give_median(self):
        base = BASE
        # 6 пар: пуск uav → через 15 мин исход (impact).
        for i in range(6):
            start = base + i * 3600
            _add_event(self.db, start, "uav", "movement")
            _add_event(self.db, start + 900, "uav", "impact")
        ev = estimate_event_eta(self.db, "kyivska")
        self.assertTrue(ev["available"])
        self.assertEqual(ev["samples"], 6)
        self.assertEqual(ev["median_seconds"], 900.0)
        text = build_eta_text(self.db, "kyivska", "Київська область")
        self.assertIn("Медіана", text)
        self.assertIn("орієнтовно", text)

    def test_explosion_class_counts_as_outcome(self):
        base = BASE
        # Исход не только stage=impact, но и класс explosion («чутко вибухи»).
        for i in range(5):
            start = base + i * 3600
            _add_event(self.db, start, "shahed", "launch")
            _add_event(self.db, start + 1200, "explosion", "movement")
        ev = estimate_event_eta(self.db, "kyivska")
        self.assertTrue(ev["available"], "вибух/ППО клас має зараховуватись як вихід")
        self.assertEqual(ev["median_seconds"], 1200.0)

    def test_risk_when_pairs_are_sparse(self):
        base = BASE
        # 15 пусков, из них 3 с исходом в горизонте 30 мин, остальные — без.
        for i in range(15):
            start = base + i * 7200
            _add_event(self.db, start, "uav", "movement")
            if i < 3:
                _add_event(self.db, start + 600 + i * 300, "explosion", "movement")
        risk = estimate_event_risk(self.db, "kyivska")
        self.assertTrue(risk["available"])
        self.assertEqual(risk["samples"], 15)
        self.assertEqual(risk["successes"], 3)
        # Лаплас: (3+1)/(15+2) ≈ 0.235.
        self.assertAlmostEqual(risk["probability"], 4 / 17, places=6)
        # Пар (5+) нет — ETA-ступень пропущена, показан риск.
        ev = estimate_event_eta(self.db, "kyivska")
        self.assertFalse(ev["available"])
        text = build_eta_text(self.db, "kyivska", "Київська область")
        self.assertIn("ймовірн", text.lower())
        self.assertIn("статистична", text)

    def test_one_launch_one_impact_only(self):
        """Один исход не создаёт оценку (порог честный)."""
        base = BASE
        _add_event(self.db, base, "uav", "movement")
        _add_event(self.db, base + 900, "explosion", "movement")
        ev = estimate_event_eta(self.db, "kyivska")
        self.assertFalse(ev["available"])
        risk = estimate_event_risk(self.db, "kyivska")
        self.assertFalse(risk["available"])


if __name__ == "__main__":
    unittest.main()
