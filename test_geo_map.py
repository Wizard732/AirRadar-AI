# -*- coding: utf-8 -*-
"""Тесты: города на карте (city_coords), origin/destination в API,
репорты угроз с геопозицией (geo_report + database + bot_ui меню)."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from database import Database
import city_coords
import health_server
import geo_report


class CityCoordsTests(unittest.TestCase):
    """Таблица городов: детект по тексту, ближайшее место."""

    def test_hostomel_detected(self):
        self.assertEqual(city_coords.detect_city("Шахеди над Гостомелем"), "hostomel")
        self.assertEqual(city_coords.detect_city("БпЛА у районі Ірпеня"), "irpin")
        self.assertEqual(city_coords.detect_city("у Бучі вибухи"), "bucha")

    def test_no_city(self):
        self.assertEqual(city_coords.detect_city("Тривога у Києві"), "")

    def test_city_region_consistency(self):
        for slug, (name, region, lat, lon, keys) in city_coords.CITIES.items():
            self.assertIn(region, city_coords.REGION_CENTROIDS, slug)
            self.assertTrue(keys, slug)

    def test_nearest_place_city(self):
        # Центр Гостомеля: ближайший город — Гостомель.
        self.assertEqual(city_coords.nearest_place(50.530, 30.262), "Гостомель")

    def test_nearest_place_falls_back_to_region(self):
        # Точка далеко от городов — имя области.
        name = city_coords.nearest_place(50.0, 33.0, max_km=25.0)
        self.assertIsInstance(name, str)
        self.assertTrue(name)


class RegionsTests(unittest.TestCase):
    def test_hostomel_maps_to_kyivska(self):
        from regions import detect_region
        self.assertIn("kyivska", detect_region("Шахеди летят над Гостомелем"))


class GeoReportDbTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_add_and_read(self):
        self.assertTrue(self.db.add_geo_report(1, 50.530123, 30.262456, text="шахед"))
        reports = self.db.recent_geo_reports(90)
        self.assertEqual(len(reports), 1)
        r = reports[0]
        self.assertEqual(r["lat"], 50.530)  # округление до ~100 м
        self.assertEqual(r["lon"], 30.262)
        self.assertNotIn("user_id", r)  # в публичный API юзер не попадает
        self.assertEqual(r["text"], "шахед")

    def test_rate_limit(self):
        self.assertTrue(self.db.add_geo_report(1, 50.5, 30.5))
        self.assertFalse(self.db.add_geo_report(1, 50.6, 30.6))
        # Другой юзер — не заблокирован.
        self.assertTrue(self.db.add_geo_report(2, 50.6, 30.6))
        wait = self.db.seconds_until_geo_report_allowed(1)
        self.assertLessEqual(wait, geo_report.Database.GEO_REPORT_COOLDOWN_S)
        self.assertGreater(wait, 280)

    def test_old_reports_not_returned(self):
        self.assertTrue(self.db.add_geo_report(1, 50.5, 30.5))
        # Прямым апдейтом старим репорт.
        with self.db._lock:
            self.db._conn.execute("UPDATE geo_reports SET ts = ts - 7200")
            self.db._conn.commit()
        self.assertEqual(self.db.recent_geo_reports(90), [])


class ActiveThreatsRouteTests(unittest.TestCase):
    """origin/destination должны приходить из active_threats (SQL incidents)."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_active_threats_has_origin_destination(self):
        # Инцидент пишется тем же методом, что и в прод-пайплайне (main.py):
        # merge_incident_fact сохраняет маршрут «з X на Y» в incidents.
        from incident_fusion import extract_incident_fact
        text = "БпЛА зі Сумської на Київщину"
        fact = extract_incident_fact(text, "uav", "imminent")
        self.db.merge_incident_fact(
            event_ts=int(time.time()), source="t", source_group="t",
            fact=fact, text=text,
        )
        rows = self.db.active_threats(within_seconds=600, include_reported=True)
        self.assertTrue(rows)
        self.assertIn("origin", rows[0])
        self.assertIn("destination", rows[0])
        self.assertEqual(rows[0]["origin"], "sumska")
        self.assertEqual(rows[0]["destination"], "kyivska")


class ApiThreatsTests(unittest.TestCase):
    """GET /api/threats: маршруты угроз + репорты пользователей в одном JSON."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        health_server.set_db(self.db)

    def tearDown(self):
        health_server.set_db(None)
        self.db.close()
        os.unlink(self.file.name)

    def test_api_threats_returns_routes_and_reports(self):
        from incident_fusion import extract_incident_fact
        text = "БпЛА зі Сумської на Київщину"
        fact = extract_incident_fact(text, "uav", "imminent")
        self.db.merge_incident_fact(
            event_ts=int(time.time()), source="t", source_group="t",
            fact=fact, text=text,
        )
        self.assertTrue(self.db.add_geo_report(7, 50.530, 30.262, text="шахед"))

        async def call():
            request = SimpleNamespace(query={"minutes": "30"})
            resp = await health_server._api_threats(request)
            return json.loads(resp.body)

        data = asyncio.run(call())
        self.assertEqual(data["window_min"], 30)
        self.assertEqual(data["count"], 1)
        threat = data["threats"][0]
        self.assertEqual(threat["origin"], "sumska")
        self.assertEqual(threat["destination"], "kyivska")
        self.assertEqual(threat["status"], "reported")
        self.assertGreaterEqual(threat["age_min"], 0)
        self.assertTrue(data["reports"], "репорты не попали в ответ API")
        self.assertNotIn("user_id", data["reports"][0])


class BotReportMenuTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        from test_bot_ui import FakeBot, FakeCallbackEvent
        from bot_ui import register_handlers
        self.FakeCallbackEvent = FakeCallbackEvent
        self.bot = FakeBot()
        register_handlers(self.bot, self.db, 111)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_report_button_in_menu(self):
        from bot_ui import _main_menu_kb
        from test_bot_ui import _button_datas
        datas = [d for row in _main_menu_kb() for d in _button_datas([row])]
        self.assertIn("report", datas)

    def test_report_callback_sends_hint(self):
        ev = self.FakeCallbackEvent(b"report", 111)
        asyncio.run(self.bot.dispatch_callback(ev))
        self.assertTrue(ev.responses, "подсказка не отправлена")
        self.assertIn("геопозицію", ev.responses[0])


class GeoReportHandlerTests(unittest.TestCase):
    """Обработчик geo_report: принимает локацию, отвечает, уважает антиспам."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        from test_bot_ui import FakeBot
        self.bot = FakeBot()
        geo_report.register_geo_report_handlers(self.bot, self.db)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _dispatch(self, message):
        handlers = [fn for _, _, fn in self.bot.handlers
                    if getattr(fn, "__name__", "") == "_geo_received"]
        ev = SimpleNamespace(message=message, sender_id=42,
                             is_private=lambda: True, respond=message.respond)
        asyncio.run(handlers[0](ev))

    def test_geo_message_creates_report(self):
        replies = []

        async def respond(text, parse_mode=None):
            replies.append(text)

        geo = SimpleNamespace(lat=50.5301, long=30.2624)
        msg = SimpleNamespace(geo=geo, text="", respond=respond)
        self._dispatch(msg)
        self.assertEqual(len(self.db.recent_geo_reports(90)), 1)
        self.assertIn("Дякуємо", replies[0])
        self.assertIn("Гостомель", replies[0])  # ближайшее место

    def test_geo_spam_blocked(self):
        replies = []

        async def respond(text, parse_mode=None):
            replies.append(text)

        geo = SimpleNamespace(lat=50.53, long=30.26)
        self._dispatch(SimpleNamespace(geo=geo, text="", respond=respond))
        self._dispatch(SimpleNamespace(geo=geo, text="", respond=respond))
        self.assertEqual(len(self.db.recent_geo_reports(90)), 1)
        self.assertIn("Ти вже надіслав", replies[-1])


if __name__ == "__main__":
    unittest.main()
