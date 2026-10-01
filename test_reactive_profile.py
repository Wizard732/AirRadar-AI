"""Тесты профиля скорости «реактивний БпЛА» (без нового slug).

Ключевые инварианты:
  • detect_speed_profile срабатывает только на uav/shahed + маркер «реактивн»;
  • РСЗО-контекст («реактивні системи залпового огня» → mlrs) профиль не
    включает — guard по классу;
  • скорость/ETA подтипа: 550 км/ч и ~10–30 хв вместо 180 км/ч и ~10–40 хв;
  • IncidentFact проставляет профиль на лету, БД и slug не меняются;
  • каскад проведён во все 3 уровня: коридор трека, вектор, типовий підліт.
"""

import os
import tempfile
import unittest

from city_coords import REGION_CENTROIDS, _haversine_km
from database import Database
from eta import vector_eta_text
from incident_fusion import IncidentFact, extract_incident_fact
from track_speed import TRACK_SPEED_MAX_KMH, TRACK_SPEED_MIN_KMH, build_track
from weapon_classes import (
    REACTIVE_KMH,
    REACTIVE_ETA,
    detect_speed_profile,
    weapon_eta,
    weapon_speed_kmh,
)
from alert_renderer import render_evidence_alert

# Конотоп → Бровари: ~188 км за 1 час = ~188 км/ч (для reactive — НИЖЕ
# нижней границы 250, для базового uav — середина коридора).
BASE = 1_735_488_000
KONOTOP = (51.243, 33.207)
BROVARY = (50.511, 30.790)
KM_KONOTOP_BROVARY = _haversine_km(KONOTOP, BROVARY)


def _add_sighting(db: Database, ts: int, text: str,
                  region: str = "kyivska", weapon: str = "uav") -> None:
    db.add_event(
        event_ts=ts, weapon_class=weapon, stage="movement", region=region,
        text=text, source="test", outcome="unknown", confidence=0.8,
    )


class DetectSpeedProfileTests(unittest.TestCase):
    def test_reactive_marker_on_uav_classes(self):
        """Маркер «реактивн» на uav/shahed → reactive, все склонения."""
        self.assertEqual(detect_speed_profile("Реактивний БпЛА курсом на Київ", "uav"), "reactive")
        self.assertEqual(detect_speed_profile("3х Реактивні БпЛА на Васильків", "uav"), "reactive")
        self.assertEqual(detect_speed_profile("реактивные дроны в воздухе", "uav"), "reactive")
        self.assertEqual(detect_speed_profile("Реактивний шахед на підході", "shahed"), "reactive")

    def test_guard_by_class_blocks_non_uav_context(self):
        """Guard: РСЗО/артиллерия/ракеты не получают профиль от слова «реактивн»."""
        self.assertEqual(detect_speed_profile("Реактивні системи залпового огня", "mlrs"), "")
        self.assertEqual(detect_speed_profile("Работа реактивной артиллерии", "artillery"), "")
        self.assertEqual(detect_speed_profile("Реактивна крылатая ракета", "cruise_missile"), "")

    def test_no_marker_or_empty_text(self):
        """Без маркера или без текста — базовый профиль."""
        self.assertEqual(detect_speed_profile("БпЛА на підході до Броварів", "uav"), "")
        self.assertEqual(detect_speed_profile("", "uav"), "")
        self.assertEqual(detect_speed_profile("Реактивний БпЛА", ""), "")


class WeaponParamsTests(unittest.TestCase):
    def test_reactive_speed_and_eta(self):
        """Профиль reactive: 550 км/ч и ~10–30 хв; без профиля — старые значения."""
        self.assertEqual(weapon_speed_kmh("uav", "reactive"), 550.0)
        self.assertEqual(weapon_speed_kmh("shahed", "reactive"), 550.0)
        self.assertEqual(weapon_eta("uav", "reactive"), "~10–30 хв")
        # Регресс базового поведения (профиль пуст или класс вне подтипа).
        self.assertEqual(weapon_speed_kmh("uav"), 180.0)
        self.assertEqual(weapon_eta("uav"), "~10–40 хв")
        self.assertEqual(weapon_speed_kmh("mlrs", "reactive"), weapon_speed_kmh("mlrs"))
        self.assertEqual(REACTIVE_KMH, {"uav": 550.0, "shahed": 550.0})
        self.assertEqual(REACTIVE_ETA, {"uav": "~10–30 хв", "shahed": "~10–30 хв"})
        # Константы коридора не потеряны.
        self.assertEqual(TRACK_SPEED_MIN_KMH, 15.0)
        self.assertEqual(TRACK_SPEED_MAX_KMH, 1500.0)


class IncidentFactProfileTests(unittest.TestCase):
    def test_extract_incident_fact_sets_profile(self):
        """extract_incident_fact проставляет профиль из текста."""
        fact = extract_incident_fact("Реактивний Бпла курсом на Київ", "uav", "imminent")
        self.assertEqual(fact.weapon_class, "uav")  # slug не меняется
        self.assertEqual(fact.speed_profile, "reactive")
        plain = extract_incident_fact("БпЛА на підході до Броварів", "uav", "imminent")
        self.assertEqual(plain.speed_profile, "")

    def test_direct_incident_fact_default(self):
        """Прямой конструктор без поля работает (дефолт '') — совместимость."""
        fact = IncidentFact("uav", "imminent", "sumska", "kyivska", "exact", 2, False, "")
        self.assertEqual(fact.speed_profile, "")


class VectorEtaProfileTests(unittest.TestCase):
    def test_vector_eta_uses_reactive_speed(self):
        """Уровень 2: vector_eta_text считает по 550 км/ч, а не по 180."""
        # Сумська → Київська: с коэффициентом маршрута 1.25 при 550 км/ч
        # минуты считаются из констант; при 180 км/ч итог > 90 мин → ''.
        km = _haversine_km(REGION_CENTROIDS["sumska"], REGION_CENTROIDS["kyivska"]) * 1.25
        expected_minutes = round(km / 550.0 * 60)
        base = IncidentFact("uav", "imminent", "sumska", "kyivska", "exact", None, False, "")
        reactive = IncidentFact("uav", "imminent", "sumska", "kyivska", "exact", None, False, "",
                                speed_profile="reactive")
        text_base = vector_eta_text(base)
        text_reactive = vector_eta_text(reactive)
        self.assertTrue(text_reactive)
        self.assertIn("орієнтовно", text_reactive)
        self.assertEqual(text_base, "")  # 180 км/ч → вне коридора 2–90 мин
        import re as _re
        m = _re.search(r"≈(\d+) хв", text_reactive)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), expected_minutes)


class TrackSpeedProfileTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_reactive_chain_in_range_available(self):
        """Реактивная цепочка 376 км/ч (Конотоп→Ніжин→Бровари за 30 мин) — в коридоре."""
        chain = [
            (BASE, "БпЛА відмічено в Конотопі"),
            (BASE + 900, "Цілі над Ніжином"),
            (BASE + 1800, "Шахеди на підході до Броварів"),
        ]
        for ts, text in chain:
            _add_sighting(self.db, ts, text)
        track = build_track(self.db, "kyivska", "uav", BASE + 1800, speed_profile="reactive")
        self.assertTrue(track["available"])
        self.assertGreaterEqual(track["speed_kmh"], 250.0)

    def test_slow_chain_rejected_for_reactive(self):
        """Уровень 1: медленная цепочка (~188 км/ч) для reactive бракуется,
        для базового профиля остаётся доступной."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 3600, "БпЛА на підході до Броварів")
        reactive = build_track(self.db, "kyivska", "uav", BASE + 3600, speed_profile="reactive")
        self.assertFalse(reactive["available"])
        self.assertIsNone(reactive["minutes"])
        base = build_track(self.db, "kyivska", "uav", BASE + 3600)
        self.assertTrue(base["available"])  # дефолт не изменился

    def test_default_profile_unchanged(self):
        """Дефолтный вызов без параметра — прежнее поведение (регресс)."""
        _add_sighting(self.db, BASE, "БпЛА відмічений в Конотопі")
        _add_sighting(self.db, BASE + 3600, "БпЛА на підході до Броварів")
        track = build_track(self.db, "kyivska", "uav", BASE + 3600)
        self.assertTrue(track["available"])
        self.assertAlmostEqual(track["speed_kmh"], round(KM_KONOTOP_BROVARY, 1), delta=2.0)


class AlertRendererProfileTests(unittest.TestCase):
    def test_reactive_header_suffix_and_typical_eta(self):
        """Шапка помечает подтип, фолбэк каскада даёт ~10–30 хв."""
        fact = IncidentFact("uav", "imminent", "", "kyivska", "exact", None, False, "",
                            speed_profile="reactive")
        text = render_evidence_alert(
            text="Реактивний БпЛА курсом на Київ", source="test",
            event_ts=BASE, fact=fact, confirmation={"status": "corroborated", "key": "k"},
            track=None,
        )
        self.assertIn("БПЛА (реактивний)", text)
        self.assertIn("Типовий підліт: ~10–30 хв", text)
        self.assertIn("орієнтовно", text)

    def test_base_profile_header_unchanged(self):
        """Без профиля — прежняя шапка и прежний диапазон (регресс)."""
        fact = IncidentFact("uav", "imminent", "", "kyivska", "exact", None, False, "")
        text = render_evidence_alert(
            text="БпЛА курсом на Київ", source="test",
            event_ts=BASE, fact=fact, confirmation={"status": "corroborated", "key": "k"},
            track=None,
        )
        self.assertNotIn("(реактивний)", text)
        self.assertIn("Типовий підліт: ~10–40 хв", text)

    def test_cascade_priority_preserved_for_reactive(self):
        """Каскад: трек (уровень 1) по-прежнему выше типового підліту."""
        fact = IncidentFact("uav", "imminent", "", "kyivska", "exact", None, False, "",
                            speed_profile="reactive")
        track = {
            "available": True, "points": 3,
            "from_name": "Конотоп", "to_name": "Бровари",
            "from": list(KONOTOP), "to": list(BROVARY),
            "km": round(KM_KONOTOP_BROVARY, 1), "speed_kmh": 376.2,
            "course_deg": 245, "minutes": 4, "region": "kyivska",
        }
        text = render_evidence_alert(
            text="Реактивний БпЛА", source="test",
            event_ts=BASE, fact=fact, confirmation={"status": "corroborated", "key": "k"},
            track=track,
        )
        self.assertIn("За треком: Конотоп → Бровари", text)
        self.assertNotIn("Типовий підліт", text)


if __name__ == "__main__":
    unittest.main()
