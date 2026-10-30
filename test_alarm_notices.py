import asyncio
import os
import tempfile
import time
import unittest

import alarm_notices as an
from database import Database


class Pub:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return {"chat": {"id": "c"}, "message_id": len(self.sent)}


class AlarmTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(an.classify("🚨 Повітряна тривога в Харківській області")[0], "start")
        self.assertEqual(an.classify("Тривога в Києві")[0:2], ("start", ["kyivska"]))
        self.assertEqual(an.classify("Відбій повітряної тривоги в Харківській області")[0], "end")
        for noise in ("В області по всьому тиша. Бережіть себе, не ігноруйте повітряні тривоги.",
                      "Відбій давати не хочуть", "На 03:30 по всьому чисто", "⚪️Відбій загрози застосування балістичного озброєння",
                      "Повітряна тривога (без області)", "Станом на 03:00 тривога в Київській області"):
            self.assertEqual(an.classify(noise)[0], "", noise)

    def test_state_machine_posts_once_and_end_has_duration(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close()
        db = Database(f.name); pub = Pub(); notified = []
        async def notify(regs, text): notified.append((regs, text))
        now = int(time.time())
        run = lambda coro: asyncio.run(coro)
        self.assertEqual(run(an.apply(db, pub, notify, "start", ["kharkivska"], now - 600, now)), "start")
        self.assertEqual(run(an.apply(db, pub, notify, "start", ["kharkivska"], now - 500, now)), "", "повтор з іншого каналу")
        self.assertEqual(run(an.apply(db, pub, notify, "end", ["sumska"], now - 400, now)), "", "відбій без відкритої тривоги")
        self.assertEqual(run(an.apply(db, pub, notify, "end", ["kharkivska"], now - 60, now)), "end")
        self.assertEqual(len(pub.sent), 2)
        self.assertIn("ПОВІТРЯНА ТРИВОГА", pub.sent[0]); self.assertIn("тривала 9 хв", pub.sent[1])
        self.assertEqual(len(notified), 2)
        self.assertEqual(run(an.apply(db, pub, notify, "start", ["kharkivska"], now - 3600, now)), "", "стара подія (дозавантаження) не постимо")
        db.close(); os.unlink(f.name)

    def test_old_standdown_suppression(self):
        self.assertTrue(an.suppress_old_standdown("Відбій давати не хочуть"))
        self.assertTrue(an.suppress_old_standdown("Відбій повітряної тривоги в Сумській області"))
        self.assertFalse(an.suppress_old_standdown("⚪️Відбій загрози застосування балістичного озброєння"))


if __name__ == "__main__":
    unittest.main()
