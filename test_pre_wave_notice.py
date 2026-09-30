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


if __name__ == "__main__":
    unittest.main()
