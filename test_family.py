import asyncio
import os
import tempfile
import time
import unittest

import family
import guard_places
import proximity
from database import Database
from test_guard_places import HOME, obs


class FamilyTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_invite_accept_mutual_and_rules(self):
        family.remember_name(self.db, 1, "Андрій"); family.remember_name(self.db, 2, "Оля")
        code = family.create_invite(self.db, 1)
        self.assertEqual(family.accept_invite(self.db, code, 1)[0], False, "своє запрошення")
        ok, msg, owner = family.accept_invite(self.db, code, 2)
        self.assertTrue(ok); self.assertEqual(owner, 1)
        self.assertEqual(family.members(self.db, 1), [2]); self.assertEqual(family.members(self.db, 2), [1])
        self.assertFalse(family.accept_invite(self.db, code, 3)[0], "одноразове")
        old = family.create_invite(self.db, 1, now=int(time.time()) - 3 * 86400)
        self.assertIn("застаріло", family.accept_invite(self.db, old, 3)[1])
        self.assertTrue(family.remove_member(self.db, 2, 1))
        self.assertEqual(family.members(self.db, 1), [])

    def test_family_gets_danger_and_safe_messages_and_safe_stops_repeats(self):
        now = int(time.time())
        family.remember_name(self.db, 1, "Андрій"); family.remember_name(self.db, 2, "Оля")
        ok, _, _ = family.accept_invite(self.db, family.create_invite(self.db, 1), 2)
        guard_places.add_place(self.db, 1, "🏠 Дім", *HOME)
        for ts, d in ((now - 600, 32), (now - 330, 20), (now - 60, 8.5)):
            obs(self.db, ts, d, HOME)
        log = []
        async def send(uid, text, **kw): log.append((uid, text))
        asyncio.run(proximity.evaluate_once(self.db, send, now, ""))
        to2 = [t for u, t in log if u == 2]
        self.assertEqual(len(to2), 1); self.assertIn("Андрій", to2[0]); self.assertIn("🏠 Дім", to2[0])
        self.assertNotIn("50.4", to2[0], "координат у повідомленні родині немає")
        # Андрій відмітив «в укритті» → родина отримує, повторів немає
        family.set_safe(self.db, 1, now)
        self.assertTrue(family.is_safe(self.db, 1, now))
        self.assertIn("в укритті", family.safe_text(self.db, 1))
        self.assertEqual(family.is_safe(self.db, 1, now + family.SAFE_TTL_S + 5), False)

    def test_onboarding_flag(self):
        self.assertTrue(family.needs_onboarding(self.db, 9))
        family.mark_onboarded(self.db, 9)
        self.assertFalse(family.needs_onboarding(self.db, 9))


if __name__ == "__main__":
    unittest.main()
