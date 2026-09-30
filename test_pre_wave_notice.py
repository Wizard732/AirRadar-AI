# -*- coding: utf-8 -*-
"""Тесты проактивного уведомления «можлива нова тривога» (статистика волн).

Проверяем: окно срабатывания вокруг медианы паузы, защиту от повторной
отправки на тот же эпизод отбоя, молчание при активной тревоге / мало
данных / вне окна, и что фоновый проход _check_pre_wave реально шлёт
текст подписчику и записывает wave_notice.
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
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, user_id, text, link_preview=False, buttons=None):
        self.sent.append((user_id, text))


class PreWaveNoticeTests(unittest.TestCase):
    """pre_wave_notice: окно срабатывания и честный отказ."""

    # Как в test_wave_forecast: медиана паузы «відбій → старт» = 50 мин
    # (EPISODE_SPACING 3900 + FLIGHT_TO_ALERT 300 − FLIGHT_TO_END 1200).
    def _seed_region(self, db, region: str, count: int = 4) -> int:
        base = int(time.time()) - 10 * 86400
        flight_to_alert, flight_to_end, spacing = 300, 1200, 3900
        last_end = 0
        for i in range(count):
            flight = base + i * spacing
            db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                         region=region, text=f"БпЛА на {region} {i}", source="t")
            db.alert_start(region, event_ts=flight + flight_to_alert)
            db.alert_end(region, event_ts=flight + flight_to_end)
            last_end = flight + flight_to_end
        return last_end

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_notify_inside_window(self):
        """Медиана 50 мин → за 5 мин до срока окно открыто."""
        last_end = self._seed_region(self.db, "sumska")
        now = last_end + 45 * 60  # за 5 мин до медианы (LEAD=10 мин)
        info = wave_forecast.pre_wave_notice(self.db, "sumska", now=now)
        self.assertTrue(info["notify"])
        self.assertEqual(info["episode_end_ts"], last_end)
        self.assertEqual(info["median_minutes"], 50)
        self.assertEqual(info["samples"], 3)

    def test_no_notify_outside_window(self):
        """Слишком рано после отбоя / слишком поздно — молчим."""
        self._seed_region(self.db, "sumska")
        # now берём от последнего эпизода: пересеваем отдельно для каждого case.
        last_end = self._seed_region(self.db, "sumska")
        early = wave_forecast.pre_wave_notice(self.db, "sumska", now=last_end + 20 * 60)
        self.assertFalse(early["notify"])
        late = wave_forecast.pre_wave_notice(self.db, "sumska", now=last_end + 80 * 60)
        self.assertFalse(late["notify"])

    def test_no_repeat_for_same_episode(self):
        """Одно уведомление на эпизод отбоя: после отправки — молчание."""
        last_end = self._seed_region(self.db, "sumska")
        now = last_end + 45 * 60
        self.assertTrue(wave_forecast.pre_wave_notice(self.db, "sumska", now=now)["notify"])
        self.db.record_wave_notice("sumska", last_end)
        self.assertFalse(wave_forecast.pre_wave_notice(self.db, "sumska", now=now)["notify"])

    def test_silent_while_alert_active(self):
        """Активная тревога в регионе — уведомление не нужно."""
        last_end = self._seed_region(self.db, "sumska")
        self.db.alert_start("sumska", event_ts=last_end + 3600)
        now = last_end + 45 * 60
        info = wave_forecast.pre_wave_notice(self.db, "sumska", now=now)
        self.assertFalse(info["notify"])
        self.assertEqual(info["reason"], "alert_active")

    def test_no_stats_honest(self):
        """2 эпизода (< MIN_SAMPLES) — честный отказ, без гадания."""
        self._seed_region(self.db, "sumska", count=2)
        wave = wave_forecast.next_wave_estimate(self.db, "sumska")
        self.assertFalse(wave["available"])
        info = wave_forecast.pre_wave_notice(self.db, "sumska")
        self.assertFalse(info["notify"])

    def test_format_pre_wave_notice(self):
        info = {"median_minutes": 50, "samples": 3}
        text = wave_forecast.format_pre_wave_notice("sumska", info)
        self.assertIn("можлива нова тривога", text)
        self.assertIn("~50 хв", text)
        self.assertIn("статистична оцінка", text.lower())

    def test_check_pre_wave_sends_once(self):
        """Фоновый проход: подписчик получает текст, повтор — тишина."""
        db = self.db
        last_end = self._seed_region(db, "sumska")
        db.subscribe(1, "sumska")
        bot = FakeBotClient()
        now = last_end + 45 * 60
        # Мокаем time.time только внутри main._check_pre_wave: она вызывает
        # db.recently_ended_alerts() (реальное время) и pre_wave_notice(now=…).
        # Проще: проверяем поведение при реальном времени — эпизоды 10-дневной
        # давности не попадут в окно recently_ended_alerts(8ч). Поэтому
        # пересеваем свежие эпизоды, завершившиеся ~45 мин назад.
        db2 = db  # используем ту же БД: очищать нечего, регион один
        recent_end = int(time.time()) - 45 * 60
        db2.alert_start("sumska", event_ts=recent_end - 30 * 60)
        db2.alert_end("sumska", event_ts=recent_end)
        sent = asyncio.run(main._check_pre_wave(bot, db2))
        self.assertEqual(sent, 1)
        self.assertEqual(len(bot.sent), 1)
        user_id, text = bot.sent[0]
        self.assertEqual(user_id, 1)
        self.assertIn("можлива нова тривога", text)
        # Повторный проход — уведомление уже записано.
        sent2 = asyncio.run(main._check_pre_wave(bot, db2))
        self.assertEqual(sent2, 0)
        self.assertEqual(len(bot.sent), 1)


class StanddownNoticeTests(unittest.TestCase):
    """standdown_notice: «відбій орієнтовно за ~N хв» во время тревоги."""

    # Завершённые эпизоды с гэпом «полёт → отбой» = 1200с (медиана 20 мин).
    def _seed_history(self, db, region: str, count: int = 4,
                      flight_to_end: int = 1200) -> None:
        base = int(time.time()) - 10 * 86400
        for i in range(count):
            flight = base + i * 3900
            db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                         region=region, text=f"БпЛА на {region} {i}", source="t")
            db.alert_start(region, event_ts=flight + 300)
            db.alert_end(region, event_ts=flight + flight_to_end)

    def _fresh_flight(self, db, region: str, elapsed_s: int, now: int) -> int:
        """Полётный пост elapsed_s секунд назад + активная тревога региона."""
        flight = now - elapsed_s
        db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                     region=region, text="Шахеди в повітрі", source="t")
        db.alert_start(region, event_ts=flight - 300)
        return flight

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_notify_when_remaining_below_lead(self):
        """Медиана 20 мин, полёт 5 мин назад → «відбій орієнтовно за ~15 хв»."""
        self._seed_history(self.db, "sumska")
        now = int(time.time())
        self._fresh_flight(self.db, "sumska", 5 * 60, now)
        info = wave_forecast.standdown_notice(self.db, "sumska", now=now)
        self.assertTrue(info["notify"])
        self.assertEqual(info["remaining_min"], 15)
        self.assertEqual(info["median_minutes"], 20)
        # 4 засеянных эпизода: поиск полёта идёт и ДО старта тревоги
        # (FLIGHT_PRE_ALERT_SLACK_S), поэтому все 4 дают валидный гэп.
        self.assertEqual(info["samples"], 4)

    def test_silent_before_lead(self):
        """Медиана 60 мин, прошло 5 мин → остаток 55 мин > порога — молчим."""
        self._seed_history(self.db, "sumska", flight_to_end=3600)
        now = int(time.time())
        self._fresh_flight(self.db, "sumska", 5 * 60, now)
        info = wave_forecast.standdown_notice(self.db, "sumska", now=now)
        self.assertFalse(info["notify"])
        self.assertEqual(info["reason"], "too_early")

    def test_no_repeat_for_same_flight(self):
        """Одно сообщение на эпизод полёта: после отправки — молчание."""
        self._seed_history(self.db, "sumska")
        now = int(time.time())
        flight = self._fresh_flight(self.db, "sumska", 5 * 60, now)
        info = wave_forecast.standdown_notice(self.db, "sumska", now=now)
        self.assertTrue(info["notify"])
        self.db.record_standdown_notice("sumska", flight)
        again = wave_forecast.standdown_notice(self.db, "sumska", now=now)
        self.assertFalse(again["notify"])
        self.assertEqual(again["reason"], "already_sent")

    def test_new_flight_starts_new_episode(self):
        """Новый полётный пост = новый эпизод: сообщение можно прислать снова."""
        self._seed_history(self.db, "sumska")
        now = int(time.time())
        flight1 = self._fresh_flight(self.db, "sumska", 5 * 60, now)
        self.db.record_standdown_notice("sumska", flight1)
        # Свежий полётный пост через несколько минут (числится позже).
        flight2 = flight1 + 240
        self.db.add_event(event_ts=flight2, weapon_class="uav", stage="movement",
                          region="sumska", text="Ще група БпЛА", source="t")
        info = wave_forecast.standdown_notice(self.db, "sumska", now=flight2 + 60)
        self.assertTrue(info["notify"])

    def test_silent_when_no_active_alert(self):
        """Тревога закрыта (отбой) — countdown не нужен."""
        self._seed_history(self.db, "sumska")
        now = int(time.time())
        flight = self._fresh_flight(self.db, "sumska", 5 * 60, now)
        self.db.alert_end("sumska", event_ts=now)
        info = wave_forecast.standdown_notice(self.db, "sumska", now=now + 60)
        self.assertFalse(info["notify"])
        self.assertEqual(info["reason"], "no_alert")
        _ = flight

    def test_silent_when_no_recent_flight(self):
        """Тревога активна, но целей в воздухе (3ч) нет — молчим."""
        self._seed_history(self.db, "sumska")  # эпизоды 10-дневной давности
        self.db.alert_start("sumska", event_ts=int(time.time()) - 600)
        info = wave_forecast.standdown_notice(self.db, "sumska")
        self.assertFalse(info["notify"])
        self.assertEqual(info["reason"], "no_flight")

    def test_no_stats_honest(self):
        """2 эпизода (< MIN_SAMPLES) — честный отказ без гадания."""
        self._seed_history(self.db, "sumska", count=2)
        now = int(time.time())
        self._fresh_flight(self.db, "sumska", 5 * 60, now)
        info = wave_forecast.standdown_notice(self.db, "sumska", now=now)
        self.assertFalse(info["notify"])
        self.assertEqual(info["reason"], "no_stats")

    def test_format_standdown_notice(self):
        info = {"remaining_min": 15, "any_minute_now": False, "median_minutes": 20,
                "elapsed_minutes": 5, "samples": 3}
        text = wave_forecast.format_standdown_notice("sumska", info)
        self.assertIn("відбій орієнтовно за ~15 хв", text)
        self.assertIn("Чому:", text)
        self.assertIn("~5 хв тому", text)
        self.assertIn("~20 хв", text)
        self.assertIn("статистична оцінка", text.lower())

    def test_check_standdown_sends_once(self):
        """Фоновый проход: подписчик получает countdown, повтор — тишина."""
        db = self.db
        self._seed_history(db, "sumska")
        db.subscribe(1, "sumska")
        now = int(time.time())
        self._fresh_flight(db, "sumska", 5 * 60, now)
        bot = FakeBotClient()
        sent = asyncio.run(main._check_standdown(bot, db))
        self.assertEqual(sent, 1)
        self.assertEqual(len(bot.sent), 1)
        user_id, text = bot.sent[0]
        self.assertEqual(user_id, 1)
        self.assertIn("відбій орієнтовно", text)
        self.assertIn("Чому:", text)
        # Повторный проход — уведомление уже записано.
        sent2 = asyncio.run(main._check_standdown(bot, db))
        self.assertEqual(sent2, 0)
        self.assertEqual(len(bot.sent), 1)


if __name__ == "__main__":
    unittest.main()
