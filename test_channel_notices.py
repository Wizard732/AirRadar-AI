# -*- coding: utf-8 -*-
"""Прогнозы волн видны и в канале, а не только в ЛС подписчиков.

Правило продукта: «відбій орієнтовно за ~N хв» и «можлива нова тривога»
публикуются в целевой канал тем же текстом, что уходит подписчикам.
Без бота-меню (bot_client=None) прогнозы в канал всё равно уходят.
"""

import asyncio
import os
import tempfile
import time
import unittest

from database import Database

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


class FakePublisher:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, *a, **kw):
        self.sent.append(text)
        return {"chat": {"id": "chat"}, "message_id": 1}

    async def edit(self, chat_id, message_id, text: str, *a, **kw):
        return True


class ChannelNoticeTests(unittest.TestCase):
    """_check_standdown / _check_pre_wave публикуют прогноз в канал."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()
        self.publisher = FakePublisher()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _seed_history(self, db, region: str, count: int = 4) -> None:
        """Завершённые эпизоды: медиана «полёт → отбой» = 20 мин."""
        base = int(time.time()) - 10 * 86400
        for i in range(count):
            flight = base + i * 3900
            db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                         region=region, text=f"БпЛА на {region} {i}", source="t")
            db.alert_start(region, event_ts=flight + 300)
            db.alert_end(region, event_ts=flight + 1200)

    def test_standdown_goes_to_channel(self):
        """Countdown отбоя уходит и подписчику, и в канал."""
        self._seed_history(self.db, "sumska")
        self.db.subscribe(1, "sumska")
        now = int(time.time())
        flight = now - 5 * 60
        self.db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                          region="sumska", text="Шахеди в повітрі", source="t")
        self.db.alert_start("sumska", event_ts=flight - 300)
        bot = FakeBotClient()
        sent = asyncio.run(main._check_standdown(bot, self.db, self.publisher))
        self.assertEqual(sent, 1)
        self.assertEqual(len(bot.sent), 1, "ЛС подписчику")
        self.assertEqual(len(self.publisher.sent), 1, "публикация в канал")
        self.assertIn("відбій орієнтовно", self.publisher.sent[0])
        self.assertIn("📡 Джерело: AirRadar AI", self.publisher.sent[0])

    def test_standdown_channel_only_without_bot(self):
        """Без бота-меню прогноз всё равно уходит в канал."""
        self._seed_history(self.db, "sumska")
        now = int(time.time())
        flight = now - 5 * 60
        self.db.add_event(event_ts=flight, weapon_class="uav", stage="movement",
                          region="sumska", text="Шахеди в повітрі", source="t")
        self.db.alert_start("sumska", event_ts=flight - 300)
        sent = asyncio.run(main._check_standdown(None, self.db, self.publisher))
        self.assertEqual(sent, 0, "нет бота — нет ЛС-рассылки")
        self.assertEqual(len(self.publisher.sent), 1, "канал получает прогноз")

    def test_pre_wave_goes_to_channel(self):
        """«Можлива нова тривога» уходит и подписчику, и в канал."""
        self._seed_history(self.db, "sumska")
        self.db.subscribe(1, "sumska")
        recent_end = int(time.time()) - 45 * 60
        self.db.alert_start("sumska", event_ts=recent_end - 30 * 60)
        self.db.alert_end("sumska", event_ts=recent_end)
        bot = FakeBotClient()
        sent = asyncio.run(main._check_pre_wave(bot, self.db, self.publisher))
        self.assertEqual(sent, 1)
        self.assertEqual(len(bot.sent), 1, "ЛС подписчику")
        self.assertEqual(len(self.publisher.sent), 1, "публикация в канал")
        self.assertIn("можлива нова тривога", self.publisher.sent[0])

    def test_no_publish_without_notice(self):
        """Нет повода — в канал ничего не уходит (нет спама)."""
        self._seed_history(self.db, "sumska")
        sent = asyncio.run(main._check_standdown(None, self.db, self.publisher))
        self.assertEqual(sent, 0)
        self.assertEqual(self.publisher.sent, [])


if __name__ == "__main__":
    unittest.main()
