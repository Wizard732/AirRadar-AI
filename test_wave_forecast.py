# -*- coding: utf-8 -*-
"""Тесты wave_forecast: статистика «коли відбій» и «коли наступна хвиля».

Модель проверяется на синтетической истории региона: 4 эпизода тревоги,
в каждом — «полётный» пост (movement) за 5 мин до старта тревоги и отбой
через 20 мин после полётного поста. Между отбоем и новой тревогой — 45 мин.
"""

import os
import tempfile
import time
import unittest

from database import Database
import wave_forecast

BASE = int(time.time()) - 10 * 86400  # история 10-дневной давности
FLIGHT_TO_ALERT = 300   # полётный пост → старт тревоги
FLIGHT_TO_END = 1200    # полётный пост → отбой (медиана 20 мин)
EPISODE_SPACING = 3900   # шаг между эпизодами (пауза отбой→старт = 2700с)


def _seed_episode(db, region: str, i: int) -> None:
    flight = BASE + i * EPISODE_SPACING
    start = flight + FLIGHT_TO_ALERT
    end = flight + FLIGHT_TO_END
    db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                 region=region, text=f"БпЛА на {region} {i}", source="t")
    db.alert_start(region, event_ts=start)
    db.alert_end(region, event_ts=end)


def _seed_region(db, region: str, count: int = 4) -> None:
    for i in range(count):
        _seed_episode(db, region, i)


class WaveForecastTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_standdown_median(self):
        _seed_region(self.db, "sumska")
        est = wave_forecast.standdown_estimate(self.db, "sumska")
        self.assertTrue(est["available"])
        self.assertEqual(est["samples"], 4)
        self.assertEqual(est["median_seconds"], float(FLIGHT_TO_END))

    def test_next_wave_median(self):
        _seed_region(self.db, "sumska")
        wave = wave_forecast.next_wave_estimate(self.db, "sumska")
        self.assertTrue(wave["available"])
        expected = EPISODE_SPACING + FLIGHT_TO_ALERT - FLIGHT_TO_END
        self.assertEqual(wave["median_seconds"], float(expected))
        self.assertEqual(wave["samples"], 3)  # 4 эпизода → 3 паузы

    def test_live_standdown_remaining(self):
        _seed_region(self.db, "sumska")
        now = BASE + 3 * EPISODE_SPACING + 300  # 5 мин после последнего полёта
        live = wave_forecast.live_standdown(self.db, "sumska", now=now)
        self.assertTrue(live["available"])
        self.assertEqual(live["elapsed_minutes"], 5)
        self.assertEqual(live["remaining_min"], 15)  # 20 - 5
        self.assertFalse(live["any_minute_now"])

    def test_live_standdown_any_minute_now(self):
        _seed_region(self.db, "sumska")
        now = BASE + 3 * EPISODE_SPACING + FLIGHT_TO_END + 60  # медиана исчерпана
        live = wave_forecast.live_standdown(self.db, "sumska", now=now)
        self.assertTrue(live["available"])
        self.assertTrue(live["any_minute_now"])
        self.assertEqual(live["remaining_min"], 0)

    def test_live_no_active_flight(self):
        _seed_region(self.db, "sumska")
        now = BASE + 3 * EPISODE_SPACING + 4 * 3600  # давно ничего не летит
        live = wave_forecast.live_standdown(self.db, "sumska", now=now)
        self.assertFalse(live["available"])

    def test_region_isolation(self):
        _seed_region(self.db, "sumska")
        est = wave_forecast.standdown_estimate(self.db, "odeska")
        self.assertFalse(est["available"])

    def test_too_few_episodes_honest_unavailable(self):
        _seed_region(self.db, "sumska", count=2)
        self.assertFalse(wave_forecast.standdown_estimate(self.db, "sumska")["available"])
        self.assertFalse(wave_forecast.next_wave_estimate(self.db, "sumska")["available"])

    def test_region_forecast_compact_payload(self):
        _seed_region(self.db, "sumska")
        fc = wave_forecast.region_forecast(self.db, "sumska")
        self.assertEqual(fc["standdown_min"], 20)
        # Пауза отбой→старт: 3900 + 300 - 1200 = 3000 с = 50 мин.
        self.assertEqual(fc["next_wave_min"], 50)

    def test_format_contains_honest_labels(self):
        _seed_region(self.db, "sumska")
        text = wave_forecast.format_wave_forecast(self.db, "sumska", "")
        lowered = text.lower()
        self.assertIn("відбій", lowered)
        self.assertIn("наступна", lowered)
        self.assertIn("статистична оцінка", lowered)

    def test_format_no_data(self):
        text = wave_forecast.format_wave_forecast(self.db, "odeska", "")
        self.assertIn("Недостатньо даних", text)


class ForecastApiTests(unittest.TestCase):
    """API карты: блок forecast присутствует и пуст при отсутствии данных."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        import health_server
        health_server.set_db(self.db)
        self.health_server = health_server

    def tearDown(self):
        self.health_server.set_db(None)
        self.db.close()
        os.unlink(self.file.name)

    def test_api_threats_has_forecast_key(self):
        import asyncio
        import json
        from incident_fusion import extract_incident_fact
        from types import SimpleNamespace
        fact = extract_incident_fact("БпЛА на Київщину", "uav", "imminent")
        self.db.merge_incident_fact(
            event_ts=int(time.time()), source="t", source_group="t",
            fact=fact, text="БпЛА на Київщину",
        )
        async def call():
            request = SimpleNamespace(query={"minutes": "30"})
            resp = await self.health_server._api_threats(request)
            return json.loads(resp.body)

        data = asyncio.run(call())
        self.assertIn("forecast", data)
        self.assertIsInstance(data["forecast"], dict)


if __name__ == "__main__":
    unittest.main()
