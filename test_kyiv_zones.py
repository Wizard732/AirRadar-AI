import asyncio
import os
import sys
import tempfile
import unittest

from database import Database
from regions import detect_kyiv_zone

# _notify_subscribers живёт в main.py, который тянет telethon/aiohttp.
# Импорт делаем лениво в setUpClass, чтобы понятная ошибка была видна сразу.
main = None


def _import_main():
    global main
    if main is None:
        import main as _main
        main = _main
    return main


class FakeBotClient:
    """Собирает отправки в ЛС вместо реального бота."""

    def __init__(self):
        self.sent = []  # (user_id, text, buttons)

    async def send_message(self, user_id, text, link_preview=False, buttons=None):
        self.sent.append((user_id, text, buttons))


class KyivZoneTests(unittest.TestCase):
    """detect_kyiv_zone + адресная рассылка подписчикам берегов."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    # --- detect_kyiv_zone ---
    def test_left_bank_keywords(self):
        self.assertEqual(detect_kyiv_zone("Приліт БПЛА на Дарницю"), "kyiv_left")

    def test_left_bank_livoberezhnyi(self):
        self.assertEqual(detect_kyiv_zone("цілі прямують на Лівобережний район"), "kyiv_left")

    def test_right_bank_keywords(self):
        self.assertEqual(detect_kyiv_zone("ППО працює над Оболонню"), "kyiv_right")

    def test_both_banks_or_none_is_empty(self):
        self.assertEqual(detect_kyiv_zone("Дарниця і Оболонь — укриття"), "")
        self.assertEqual(detect_kyiv_zone("Повітряна тривога у Києві"), "")
        self.assertEqual(detect_kyiv_zone(""), "")

    # --- рассылка ---
    def test_notification_routes_to_zone_subscribers(self):
        """Подписчики kyivska + лівого берега получают текст про Дарницю, правый — нет."""
        bot = FakeBotClient()
        db = self.db
        db.subscribe(1, "kyivska")
        db.subscribe(2, "kyiv_left")
        db.subscribe(3, "kyiv_right")
        db.subscribe(4, "odeska")  # другой регион — не должен получить

        text = "🔴 Київ та область | БПЛА\nПриліт на Дарницю"
        asyncio.run(main._notify_subscribers(bot, db, ["kyivska"], text))

        recipients = sorted(uid for uid, _t, _b in bot.sent)
        self.assertEqual(recipients, [1, 2])
        # Каждому — кнопка подписки на наш канал.
        for _uid, _t, buttons in bot.sent:
            self.assertTrue(buttons is not None)

    def test_notification_no_zone_goes_to_both_banks(self):
        """Если берег не определён — получают оба берега + город."""
        bot = FakeBotClient()
        db = self.db
        db.subscribe(1, "kyivska")
        db.subscribe(2, "kyiv_left")
        db.subscribe(3, "kyiv_right")

        text = "🔴 Київ та область | БПЛА\nТривога по місту"
        asyncio.run(main._notify_subscribers(bot, db, ["kyivska"], text))

        recipients = sorted(uid for uid, _t, _b in bot.sent)
        self.assertEqual(recipients, [1, 2, 3])

    def test_notification_deduplicates_user_across_regions(self):
        """Подписчик двух затронутых регионов получает алерт один раз."""
        bot = FakeBotClient()
        db = self.db
        db.subscribe(7, "odeska")
        db.subscribe(7, "mykolaivska")

        asyncio.run(main._notify_subscribers(bot, db, ["odeska", "mykolaivska"], "текст"))

        self.assertEqual([uid for uid, _t, _b in bot.sent], [7])

    def test_blocked_user_does_not_break_broadcast(self):
        """Заблокировавший бота пользователь не мешает остальным."""
        bot = FakeBotClient()
        db = self.db
        db.subscribe(5, "odeska")
        db.subscribe(6, "odeska")

        async def broken_send(uid, text, *a, **kw):
            if uid == 5:
                raise RuntimeError("blocked")
            bot.sent.append((uid, text, kw.get("buttons")))

        bot.send_message = broken_send
        asyncio.run(main._notify_subscribers(bot, db, ["odeska"], "текст"))
        self.assertEqual([uid for uid, _t, _b in bot.sent], [6])


if __name__ == "__main__":
    unittest.main()
