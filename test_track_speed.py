"""Тесты track_speed: вектор скорости цели по засечкам из threat_events.

Ключевые инварианты:
  • ≥2 разных города в окне → трек доступен, скорость/курс/ETA считаются;
  • одна засечка или повтор того же города движения не показывают;
  • нереалистичная скорость (дубликаты/несвязанные точки) бракует трек;
  • ETA вне коридора 2–90 мин не публикуется (minutes=None);
  • любая публикация — только с пометкой «орієнтовно».
"""

import os
import tempfile
import unittest

from city_coords import REGION_CENTROIDS, _haversine_km
from database import Database
from track_speed import (
    TRACK_ETA_MAX,
    TRACK_ETA_MIN,
    build_track,
    track_eta_text,
    track_payload,
)

# Конотоп (Сумская обл.) → Бровари (Киевская обл.): ~188 км, реальный
# маршрут БпЛА. Засечки с шагом 1 час дают ~188 км/ч — середина коридора.
BASE = 1_735_488_000
KONOTOP = (51.243, 33.207)
BROVARY = (50.511, 30.790)
KM_KONOTOP_BROVARY = _haversine_km(KONOTOP, BROVARY)


def _add_sighting(db: Database, ts: int, text: str,
                  region: str = "kyivska", weapon: str = "uav",
                  stage: str = "movement") -> None:
    db.add_event(
        event_ts=ts, weapon_class=weapon, stage=stage, region=region,
        text=text, source="test", outcome="unknown", confidence=0.8,
    )


class TrackSpeedTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_two_cities_give_available_track(self):
        """≥2 разных города → available + скорость/курс/ETA в коридорах."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі", stage="launch")
        _add_sighting(self.db, BASE + 3600, "БпЛА на підході до Броварів")
        track = build_track(self.db, "kyivska", "uav", BASE + 3600)
        self.assertTrue(track["available"])
        self.assertEqual(track["points"], 2)
        self.assertEqual(track["from_name"], "Конотоп")
        self.assertEqual(track["to_name"], "Бровари")
        self.assertEqual(track["from"], [KONOTOP[0], KONOTOP[1]])
        self.assertEqual(track["to"], [BROVARY[0], BROVARY[1]])
        # Расстояние/время: 188 км за 1 час.
        self.assertAlmostEqual(track["km"], round(KM_KONOTOP_BROVARY, 1), places=1)
        self.assertAlmostEqual(track["speed_kmh"], round(KM_KONOTOP_BROVARY, 1), delta=2.0)
        # Конотоп → Бровари — юго-западный курс (азимут ~244°, 0° = север).
        self.assertGreaterEqual(track["course_deg"], 200)
        self.assertLessEqual(track["course_deg"], 290)
        # ETA: от Бровар до центроида kyivska фактической скоростью — в коридоре.
        remaining = _haversine_km(BROVARY, REGION_CENTROIDS["kyivska"]) * 1.25
        expected_minutes = round(remaining / track["speed_kmh"] * 60)
        self.assertIsNotNone(track["minutes"])
        self.assertGreaterEqual(track["minutes"], TRACK_ETA_MIN)
        self.assertLessEqual(track["minutes"], TRACK_ETA_MAX)
        self.assertEqual(track["minutes"], expected_minutes)

    def test_single_sighting_unavailable(self):
        """Одна засечка — вектора нет: движение не доказано."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        track = build_track(self.db, "kyivska", "uav", BASE)
        self.assertFalse(track["available"])
        self.assertEqual(track["points"], 1)
        self.assertIsNone(track["minutes"])
        # Плюс мусорные регионы-цели бракуются до чтения БД.
        self.assertFalse(build_track(self.db, "unknown", "uav", BASE)["available"])
        self.assertFalse(build_track(self.db, "multi", "uav", BASE)["available"])
        self.assertFalse(build_track(self.db, "", "uav", BASE)["available"])

    def test_duplicate_city_unavailable(self):
        """Повтор того же города (репост) движения не добавляет."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 1800, "Знову БпЛА в Конотопі — респост")
        track = build_track(self.db, "kyivska", "uav", BASE + 1800)
        self.assertFalse(track["available"])
        self.assertEqual(track["points"], 1)

    def test_absurd_speed_unavailable(self):
        """188 км за 3 минуты — бред (несвязанные точки/дубли): трек бракуем."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 180, "БпЛА на підході до Броварів")
        track = build_track(self.db, "kyivska", "uav", BASE + 180)
        self.assertFalse(track["available"])
        self.assertEqual(track["points"], 2)
        self.assertIsNone(track["minutes"])

    def test_eta_text_requires_track_and_marks_orientatively(self):
        """track_eta_text: полный трек → строка с «орієнтовно»; иначе ''."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 3600, "БпЛА на підході до Броварів")
        track = build_track(self.db, "kyivska", "uav", BASE + 3600)
        text = track_eta_text(track)
        self.assertIn("За треком: Конотоп → Бровари", text)
        self.assertIn("орієнтовно", text)
        self.assertIn("км/год", text)
        # Трека нет / ETA вне коридора → строку не публикуем вовсе.
        self.assertEqual(track_eta_text(None), "")
        self.assertEqual(track_eta_text({"available": False, "minutes": None}), "")
        self.assertEqual(track_eta_text(dict(track, minutes=None)), "")

    def test_track_payload_none_and_dict(self):
        """track_payload: нет трека → None; есть — компактный dict без служебных полей."""
        self.assertIsNone(track_payload(None))
        self.assertIsNone(track_payload({"available": False, "points": 0}))
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 3600, "БпЛА на підході до Броварів")
        track = build_track(self.db, "kyivska", "uav", BASE + 3600)
        payload = track_payload(track)
        self.assertIsInstance(payload, dict)
        self.assertEqual(
            sorted(payload.keys()),
            ["course_deg", "from", "from_name", "minutes", "points", "speed_kmh", "to", "to_name"],
        )
        self.assertEqual(payload["speed_kmh"], track["speed_kmh"])
        self.assertEqual(payload["course_deg"], track["course_deg"])
        self.assertEqual(payload["minutes"], track["minutes"])


if __name__ == "__main__":
    unittest.main()
