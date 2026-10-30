import asyncio
import os
import tempfile
import time
import unittest

import guard_places
import menu_screens as ms
from admin_ui import register_admin_handlers
from bot_ui import register_handlers
from database import Database
from interests_ui import register_interests_handlers
from test_bot_ui import ADMIN_ID, FakeBot, FakeCallbackEvent, _button_datas


class PlacesShelterUiTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)
        self.bot = FakeBot()
        register_handlers(self.bot, self.db, ADMIN_ID, "", "https://x.github.io/R")
        register_interests_handlers(self.bot, self.db, ADMIN_ID)
        register_admin_handlers(self.bot, self.db, ADMIN_ID)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def click(self, data, uid=ADMIN_ID):
        ev = FakeCallbackEvent(data.encode(), uid)
        asyncio.run(self.bot.dispatch_callback(ev))
        return ev

    def test_add_place_by_text_with_custom_name_then_delete_via_button(self):
        ev = self.click("pla:add:home")
        self.assertIn("Напиши місто", ev.responses[0])
        label = self.bot._airradar_pending[ADMIN_ID][1]
        self.assertEqual(label, "🏠 Дім")
        ok, reply = ms.place_add_from_text(self.db, ADMIN_ID, label, "Харків")
        self.assertTrue(ok); self.assertIn("🏠 Дім", reply)
        ok, _ = ms.place_add_from_text(self.db, ADMIN_ID, label, "Мама: Суми")
        self.assertTrue(ok)
        names = [p["name"] for p in guard_places.list_places(self.db, ADMIN_ID)]
        self.assertEqual(names, ["🏠 Дім", "Мама"])
        screen = self.click("plc")
        datas = _button_datas(screen.edits[0]["buttons"])
        pid = guard_places.list_places(self.db, ADMIN_ID)[0]["id"]
        self.assertIn(f"pla:del:{pid}", datas)
        self.click(f"pla:del:{pid}")
        self.assertEqual([p["name"] for p in guard_places.list_places(self.db, ADMIN_ID)], ["Мама"])
        self.click("pla:rad:15")
        self.assertEqual(guard_places.list_places(self.db, ADMIN_ID)[0]["radius_km"], 15.0)

    def test_bad_city_rejected(self):
        ok, reply = ms.place_add_from_text(self.db, 1, "Дім", "Ъъъъъ")
        self.assertFalse(ok); self.assertIn("Не знайшов", reply)

    def test_shelter_flow_with_saved_place_and_fake_finder(self):
        guard_places.add_place(self.db, ADMIN_ID, "🏠 Дім", 50.4501, 30.5234)
        pick = self.click("shl")
        pid = guard_places.list_places(self.db, ADMIN_ID)[0]["id"]
        self.assertIn(f"shl:p{pid}", _button_datas(pick.edits[0]["buttons"]))

        async def finder(lat, lon):
            return [{"name": "Укриття №5", "kind": "укриття", "lat": lat + 0.001, "lon": lon, "m": 120, "note": ""}]
        text, rows = asyncio.run(ms.shelter_reply(50.45, 30.52, "🏠 Дім", finder=finder))
        self.assertIn("Укриття №5", text); self.assertIn("2 хв пішки", text)
        text2, rows2 = asyncio.run(ms.shelter_reply(50.45, 30.52, "", finder=lambda a, b: _none()))
        self.assertIn("недоступний", text2)
        self.assertTrue(rows2, "кнопки пошуку на карті є навіть без даних")
        # callback з точкою не падає (мережі в тесті немає → «недоступний» або знайдено)
        from unittest import mock
        import shelters
        with mock.patch.object(shelters, "nearest", finder):
            ev = self.click(f"shl:p{pid}")
        self.assertIn("Укриття №5", ev.edits[0]["text"])
        loc = self.click("shl:loc")
        self.assertEqual(self.bot._airradar_pending[ADMIN_ID][:2], ("shelter", "loc"))


async def _none():
    return None


if __name__ == "__main__":
    unittest.main()
