# -*- coding: utf-8 -*-
"""Тесты шести улучшений UX: hit-rate прогноза, профиль часов, прогресс
эпизода, персональный фильтр типов угроз, кнопка «Поділитися», и
расширенный payload region_forecast для карты.
"""

import asyncio
import os
import tempfile
import time
import unittest

from database import Database
import wave_forecast

main = None


def _import_main():
    global main
    if main is None:
        import main as _main
        main = _main
    return main


class FakeBotClient:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, user_id, text, link_preview=False, buttons=None):
        self.sent.append((user_id, text, buttons))


class AccuracyTests(unittest.TestCase):
    """standdown_hit_rate: честный leave-one-out hit-rate прогноза отбоя."""

    def _seed(self, db, region: str, gaps: list[int]) -> None:
        base = int(time.time()) - 10 * 86400
        for i, gap in enumerate(gaps):
            flight = base + i * 21600
            db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                         region=region, text=f"БпЛА {region} {i}", source="t")
            db.alert_start(region, event_ts=flight - 300)
            db.alert_end(region, event_ts=flight + gap)

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_equal_gaps_all_hits(self):
        self._seed(self.db, "sumska", [1200, 1200, 1200, 1200])
        acc = wave_forecast.standdown_accuracy(self.db, "sumska")
        self.assertTrue(acc["available"])
        self.assertEqual((acc["hits"], acc["total"]), (4, 4))

    def test_outlier_misses(self):
        """Выброс 3000с против медианы остальных 1200с — единственный промах."""
        self._seed(self.db, "sumska", [600, 1200, 1200, 3000])
        acc = wave_forecast.standdown_accuracy(self.db, "sumska")
        self.assertEqual((acc["hits"], acc["total"]), (3, 4))

    def test_too_few_episodes_unavailable(self):
        self._seed(self.db, "sumska", [1200, 1200, 1200])
        acc = wave_forecast.standdown_accuracy(self.db, "sumska")
        self.assertFalse(acc["available"])


class HourProfileTests(unittest.TestCase):
    """wave_hour_profile: часы суток, когда тревоги начинаются чаще всего."""

    def _ts_at_kyiv_hour(self, hour: int, days_ago: int = 1) -> int:
        base = int(time.time()) - days_ago * 86400
        shift = (hour - wave_forecast._kyiv_hour_of(base)) % 24
        return base + shift * 3600

    def _seed_starts(self, db, region: str, hours: list[int]) -> None:
        for i, hour in enumerate(hours):
            start = self._ts_at_kyiv_hour(hour)
            db.alert_start(region, event_ts=start)
            db.alert_end(region, event_ts=start + 600)

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_concentrated_hours_found(self):
        """Ночные старты 0:00,0:00,1:00,1:00,2:00 → окно 00:00–06:00."""
        self._seed_starts(self.db, "sumska", [0, 0, 1, 1, 2])
        prof = wave_forecast.wave_hour_profile(self.db, "sumska")
        self.assertTrue(prof["available"])
        self.assertEqual((prof["from_hour"], prof["to_hour"]), (0, 6))
        self.assertEqual(prof["samples"], 5)

    def test_scattered_hours_no_profile(self):
        self._seed_starts(self.db, "sumska", [0, 6, 12, 18, 3])
        prof = wave_hour = wave_forecast.wave_hour_profile(self.db, "sumska")
        self.assertFalse(prof["available"])

    def test_too_few_episodes(self):
        self._seed_starts(self.db, "sumska", [0, 0, 1])
        prof = wave_forecast.wave_hour_profile(self.db, "sumska")
        self.assertFalse(prof["available"])


class EpisodeProgressTests(unittest.TestCase):
    """episode_progress: «хвиля триває X, типова тривалість Y»."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _seed_history(self, region: str, count: int = 4, duration: int = 1200) -> None:
        base = int(time.time()) - 10 * 86400
        for i in range(count):
            start = base + i * 21600
            # Полётный пост внутри эпизода — без него нет gap «полёт → отбой»
            # и метрика точности честно недоступна.
            flight = start + 300
            self.db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                              region=region, text=f"БпЛА {region} {i}", source="t")
            self.db.alert_start(region, event_ts=start)
            self.db.alert_end(region, event_ts=start + duration)

    def test_active_episode_progress(self):
        self._seed_history("sumska")
        started = int(time.time()) - 80 * 60
        self.db.alert_start("sumska", event_ts=started)
        ep = wave_forecast.episode_progress(self.db, "sumska")
        self.assertTrue(ep["active"])
        self.assertEqual(ep["elapsed_min"], 80)
        self.assertEqual(ep["median_min"], 20)
        self.assertTrue(ep["over_median"])

    def test_no_active_episode(self):
        self._seed_history("sumska")
        self.assertFalse(wave_forecast.episode_progress(self.db, "sumska")["active"])

    def test_format_contains_progress_and_accuracy(self):
        self._seed_history("sumska")
        started = int(time.time()) - 80 * 60
        self.db.alert_start("sumska", event_ts=started)
        text = wave_forecast.format_wave_forecast(self.db, "sumska", "")
        self.assertIn("Хвиля триває", text)
        self.assertIn("довше типового", text)
        self.assertIn("4/4", text)
        self.assertIn("Точність", text)


class WeaponFilterTests(unittest.TestCase):
    """Персональный фильтр типов угроз: БД + фильтрация рассылки."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_roundtrip_and_validation(self):
        self.assertEqual(self.db.get_weapon_classes(1), "")
        self.db.set_weapon_classes(1, "ballistic,uav")
        self.assertEqual(self.db.get_weapon_classes(1), "ballistic,uav")
        # Неверные группы отбрасываются.
        self.db.set_weapon_classes(1, "ballistic,junk")
        self.assertEqual(self.db.get_weapon_classes(1), "ballistic")
        self.db.set_weapon_classes(1, "")
        self.assertEqual(self.db.get_weapon_classes(1), "")

    def test_notify_respects_filter(self):
        """uav заглушён, ballistic и відбій проходят при фильтре 'ballistic'."""
        db = self.db
        db.subscribe(1, "odeska")
        db.set_weapon_classes(1, "ballistic")
        bot = FakeBotClient()
        orig = main._is_night_time
        main._is_night_time = lambda: False
        try:
            asyncio.run(main._notify_subscribers(
                bot, db, ["odeska"], "🟡 ОДЕСА | БПЛА", weapon_class="uav"))
            self.assertEqual(bot.sent, [])
            asyncio.run(main._notify_subscribers(
                bot, db, ["odeska"], "🔴 ОДЕСА | Балістика", weapon_class="ballistic"))
            self.assertEqual(len(bot.sent), 1)
            bot.sent.clear()
            asyncio.run(main._notify_subscribers(
                bot, db, ["odeska"], "🟢 ВІДБІЙ — ОДЕСА — 12:00",
                weapon_class="stand_down"))
            self.assertEqual(len(bot.sent), 1)
        finally:
            main._is_night_time = orig

    def test_no_filter_delivers_everything(self):
        db = self.db
        db.subscribe(1, "odeska")
        bot = FakeBotClient()
        orig = main._is_night_time
        main._is_night_time = lambda: False
        try:
            asyncio.run(main._notify_subscribers(
                bot, db, ["odeska"], "🟡 ОДЕСА | БПЛА", weapon_class="uav"))
            self.assertEqual(len(bot.sent), 1)
        finally:
            main._is_night_time = orig

    def test_weapon_group_mapping(self):
        self.assertEqual(main.weapon_group("stand_down"), "")
        self.assertEqual(main.weapon_group(""), "")
        self.assertEqual(main.weapon_group("shahed"), "uav")
        self.assertEqual(main.weapon_group("cruise_missile"), "ballistic")
        self.assertEqual(main.weapon_group("explosion"), "other")


class ShareButtonTests(unittest.TestCase):
    """Кнопка «Поділитися» под алертом в ЛС."""

    def setUp(self):
        _import_main()

    def test_feedback_kb_has_share_row(self):
        kb = main._feedback_kb("k1", text="🟢 ВІДБІЙ — КИЇВ ТА ОБЛАСТЬ — 12:00\nдалі")
        # 3 ряда: [✅ Корисно / ➖ Шум], [❌ Помилка], [↗ Поділитися].
        self.assertEqual(len(kb), 3)
        error_btn = kb[1][0]
        self.assertIn("Помилка", getattr(error_btn, "text", ""))
        btn = kb[2][0]
        # В Telethon URL лежит в btn.type.url (InlineButtonTypeUrl).
        url = getattr(getattr(btn, "type", None), "url", "") or getattr(btn, "url", "")
        self.assertIn("t.me/share/url", url)
        self.assertIn("text=", url)  # заголовок алерта предзаполнен

    def test_feedback_kb_without_text(self):
        kb = main._feedback_kb("k1")
        # Без текста ряда «Поділитися» нет: [✅/➖], [❌ Помилка].
        self.assertEqual(len(kb), 2)
        self.assertIn("Помилка", getattr(kb[1][0], "text", ""))


class RegionForecastPayloadTests(unittest.TestCase):
    """region_forecast: новые ключи для карты (live-флаг, точность)."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_payload_includes_accuracy_and_live_flag(self):
        base = int(time.time()) - 10 * 86400
        for i in range(4):
            flight = base + i * 21600
            self.db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                              region="sumska", text=f"БпЛА {i}", source="t")
            self.db.alert_start("sumska", event_ts=flight - 300)
            self.db.alert_end("sumska", event_ts=flight + 1200)
        fc = wave_forecast.region_forecast(self.db, "sumska")
        self.assertEqual(fc["standdown_min"], 20)
        self.assertFalse(fc["standdown_live"])  # давние полёты — не live
        self.assertEqual((fc["standdown_hits"], fc["standdown_total"]), (4, 4))
        self.assertNotIn("wave_hours", fc)  # 4 эпизода < 5 — профиля нет

    def test_empty_region_minimal_payload(self):
        self.assertEqual(wave_forecast.region_forecast(self.db, "odeska"), {})


class WeaponsScreenTests(unittest.TestCase):
    """Экран «🎯 Типи тривог» в меню бота."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        from test_bot_ui import FakeBot
        from bot_ui import register_handlers
        self.bot = FakeBot()
        register_handlers(self.bot, self.db, 111)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _callback(self, data: str):
        from test_bot_ui import FakeCallbackEvent
        event = FakeCallbackEvent(data.encode("utf-8"), 111)
        asyncio.run(self.bot.dispatch_callback(event))
        return event

    def _datas(self, buttons):
        from test_bot_ui import _button_datas
        return _button_datas(buttons)

    def test_weapons_screen_renders(self):
        event = self._callback("weapons")
        self.assertIn("Типи тривог", event.edits[0]["text"])
        datas = self._datas(event.edits[0]["buttons"])
        for key in ("wt:ballistic", "wt:uav", "wt:other", "wall", "main"):
            self.assertIn(key, datas)

    def test_toggle_and_reset(self):
        self._callback("wt:uav")
        self.assertEqual(self.db.get_weapon_classes(111), "ballistic,other")
        event = self._callback("weapons")
        self.assertIn("Ракети / балістика", event.edits[0]["text"])
        self._callback("wall")
        self.assertEqual(self.db.get_weapon_classes(111), "")

    def test_last_class_cannot_be_removed(self):
        self.db.set_weapon_classes(111, "ballistic")
        event = self._callback("wt:ballistic")
        self.assertEqual(self.db.get_weapon_classes(111), "ballistic")
        self.assertTrue(any(a for a in event.answers if "хоча б один" in a))


if __name__ == "__main__":
    unittest.main()
