import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from database import Database
from proximity import decide, evaluate_once, guard_message
from threat_parser import Observation
from track_engine import Risk, destination

T0 = int(datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc).timestamp())
NOW = T0 + 1200


def obs(lat, lon, place):
    return Observation("shahed", "", 1, None, "kyivska", place, "at", lat, lon, "place")


def risk(level="imminent", dist=8.0):
    return Risk("t1", "shahed", level, dist, 180, True, 1.0, 3, 180.0, "measured", 1)


class DecideTests(unittest.TestCase):
    def test_first_contact_notifies(self):
        self.assertTrue(decide(None, risk(), NOW))

    def test_escalation_notifies_immediately(self):
        prev = {"level": "watch", "sent_ts": NOW - 5, "distance": 20.0}
        self.assertTrue(decide(prev, risk("imminent"), NOW))

    def test_same_level_is_throttled(self):
        prev = {"level": "imminent", "sent_ts": NOW - 30, "distance": 9.0}
        self.assertFalse(decide(prev, risk("imminent", 5.0), NOW))

    def test_same_level_renotifies_only_when_closer_after_wait(self):
        prev = {"level": "imminent", "sent_ts": NOW - 200, "distance": 9.5}
        self.assertTrue(decide(prev, risk("imminent", 5.0), NOW))
        self.assertFalse(decide(prev, risk("imminent", 8.5), NOW))

    def test_message_is_short_and_has_safety_tail(self):
        text = guard_message(risk("imminent"), "https://x.example")
        self.assertIn("км", text)
        self.assertIn("/radar", text)
        self.assertLess(len(text.splitlines()), 5)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.db"))
        self.sent = []

        async def send(uid, text):
            self.sent.append((uid, text))
        self.send = send
        p0 = (49.0, 31.0)
        self.head = destination(p0, 0, 60)
        pts = [obs(*p0, "A"), obs(*destination(p0, 0, 30), "B"), obs(*self.head, "C")]
        for i, o in enumerate(pts):
            self.db.add_observations(T0 + i * 600, "kiev_nebo", [o])

    def tearDown(self):
        self._dir.cleanup()

    def run_once(self, now=NOW):
        return asyncio.run(evaluate_once(self.db, self.send, now=now))

    def test_location_ignored_until_guard_enabled(self):
        self.assertFalse(self.db.update_guard_location(1, 49.0, 31.0, True, now=NOW))
        self.db.set_guard(1, True)
        self.assertTrue(self.db.update_guard_location(1, 49.0, 31.0, True, now=NOW))

    def test_live_point_expires_but_fixed_point_does_not(self):
        self.db.set_guard(1, True)
        self.db.update_guard_location(1, 49.0, 31.0, True, now=NOW - 3600)
        self.assertEqual(self.db.active_guards(NOW), [])
        self.db.update_guard_location(1, 49.0, 31.0, False, now=NOW - 3600)
        self.assertEqual(len(self.db.active_guards(NOW)), 1)

    def test_turning_guard_off_erases_coordinates(self):
        self.db.set_guard(1, True)
        self.db.update_guard_location(1, 49.0, 31.0, False, now=NOW)
        self.db.set_guard(1, False)
        self.assertIsNone(self.db.get_guard(1))

    def test_alert_sent_once_for_approaching_target(self):
        user = destination(self.head, 0, 8)
        self.db.set_guard(7, True)
        self.db.update_guard_location(7, *user, False, now=NOW)
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.sent[0][0], 7)
        self.assertIn("🟠", self.sent[0][1])
        self.assertEqual(self.run_once(NOW + 10), 0)  # антиспам

    def test_far_user_gets_nothing(self):
        self.db.set_guard(7, True)
        self.db.update_guard_location(7, *destination(self.head, 0, 120), False, now=NOW)
        self.assertEqual(self.run_once(), 0)

    def test_user_behind_target_gets_nothing(self):
        self.db.set_guard(7, True)
        self.db.update_guard_location(7, *destination(self.head, 180, 8), False, now=NOW)
        self.assertEqual(self.run_once(), 0)

    def test_retreat_message_after_target_passes(self):
        user = destination(self.head, 0, 8)
        self.db.set_guard(7, True)
        self.db.update_guard_location(7, *user, False, now=NOW)
        self.run_once()
        self.sent.clear()
        # через 10 минут цель по курсу уже ~30 км севернее и летит дальше от точки
        self.assertEqual(self.run_once(NOW + 600), 1)
        self.assertIn("віддалилась", self.sent[0][1])

    def test_failed_send_is_retried_next_pass(self):
        user = destination(self.head, 0, 8)
        self.db.set_guard(7, True)
        self.db.update_guard_location(7, *user, False, now=NOW)

        async def boom(uid, text):
            raise RuntimeError("blocked")
        self.assertEqual(asyncio.run(evaluate_once(self.db, boom, now=NOW)), 0)
        self.assertEqual(self.run_once(NOW + 5), 1)


if __name__ == "__main__":
    unittest.main()
