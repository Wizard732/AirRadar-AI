"""Каждая кнопка меню должна что-то делать: обходим все достижимые inline-кнопки.

Стартуем с главного меню (с картой/Mini App), нажимаем каждую callback-кнопку на каждом экране,
пока новые экраны не закончатся. «Мёртвая» кнопка (ни edit, ни respond, ни answer) или исключение — провал.
"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from admin_ui import register_admin_handlers
from bot_ui import RB_LEGACY, _persistent_kb, register_handlers
from database import Database
from interests_ui import register_interests_handlers
from test_bot_ui import ADMIN_ID, FakeBot, FakeCallbackEvent, FakeMessageEvent, _button_datas, _button_rows, _button_texts

MAP = "https://wizard732.github.io/AirRadar-AI"


class MenuButtonsTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close()
        self.path = f.name
        self.db = Database(self.path)
        self.bot = FakeBot()
        register_handlers(self.bot, self.db, ADMIN_ID, "", MAP)
        register_interests_handlers(self.bot, self.db, ADMIN_ID)
        register_admin_handlers(self.bot, self.db, ADMIN_ID)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _click(self, data):
        ev = FakeCallbackEvent(data.encode(), ADMIN_ID)
        asyncio.run(self.bot.dispatch_callback(ev))
        return ev

    def test_every_reachable_button_responds(self):
        seen, queue, dead = set(), ["main"], []
        while queue and len(seen) < 400:
            data = queue.pop()
            if data in seen:
                continue
            seen.add(data)
            ev = self._click(data)
            if not (ev.edits or ev.responses or ev.answers):
                dead.append(data)
            for out in [e["buttons"] for e in ev.edits] + ev.respond_buttons:
                for d in _button_datas(out):
                    if d not in seen and not d.startswith(("fb", "rsb:", "zsb:", "wt:", "sht", "night", "wall")):
                        queue.append(d)
        self.assertFalse(dead, f"кнопки без реакции: {dead}")
        for needed in ("trk", "st2:today", "st2:7d", "his", "imp", "grd", "mysubs", "active"):
            self.assertIn(needed, seen, f"экран {needed} недостижим из меню")

    def test_main_menu_has_map_and_miniapp_buttons(self):
        ev = self._click("main")
        urls = []
        for row in _button_rows(ev.edits[0]["buttons"]):
            for b in row:
                u = getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)
                if u:
                    urls.append(u)
        self.assertIn(MAP + "/", urls)
        self.assertIn(MAP + "/app.html", urls)

    def test_guard_buttons_change_state(self):
        self._click("gra:on")
        self.assertTrue(self.db.get_guard(ADMIN_ID)["enabled"])
        self._click("gra:15")
        self.assertEqual(self.db.get_guard(ADMIN_ID)["radius_km"], 15.0)
        self._click("gra:off")
        self.assertIsNone(self.db.get_guard(ADMIN_ID))

    def test_reply_keyboard_buttons_are_handled(self):
        handler = [fn for a, k, fn in self.bot.handlers if getattr(fn, "__name__", "") == "_any_text_menu"][0]
        labels = [getattr(b, 'button', b).text for row in _persistent_kb() for b in row] + list(RB_LEGACY)
        for label in labels:
            ev = FakeMessageEvent(label, ADMIN_ID)
            ev.message = SimpleNamespace(text=label, geo=None)
            ev.is_private = True
            asyncio.run(handler(ev))
            self.assertTrue(ev.responses, f"кнопка клавиатуры «{label}» не отвечает")
            self.assertNotIn("Керуйте кнопками нижче", ev.responses[0], f"«{label}» не распознана")


if __name__ == "__main__":
    unittest.main()


class CityGuardTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_city_guard_sets_fixed_point(self):
        import menu_screens
        reply = menu_screens.set_city_guard(self.db, 7, "Харкові")
        self.assertIn("Харків", reply)
        g = self.db.get_guard(7)
        self.assertTrue(g["enabled"]); self.assertAlmostEqual(g["lat"], 49.99, delta=0.1); self.assertEqual(g["live"], 0)
        self.assertIn("Не знайшов", menu_screens.set_city_guard(self.db, 7, "ЩщЩщЩщ"))

    def test_test_alert_text_uses_real_template(self):
        import menu_screens
        t = menu_screens.test_alert_text("https://x.github.io/R")
        self.assertIn("ТЕСТ", t); self.assertIn("км", t); self.assertIn("/radar", t)
