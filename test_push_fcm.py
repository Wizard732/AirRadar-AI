import asyncio
import os
import tempfile
import unittest
from unittest import mock

import push_fcm
from database import Database


class PushTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)
        self.sa = tempfile.NamedTemporaryFile(delete=False, suffix=".json"); self.sa.write(b"{}"); self.sa.close()

    def tearDown(self):
        self.db.close(); os.unlink(self.path); os.unlink(self.sa.name)

    def test_pair_rules(self):
        self.assertFalse(push_fcm.pair(self.db, 1, "bad code!")[0])
        self.assertTrue(push_fcm.pair(self.db, 1, "AbCdEf123456XY")[0])
        self.assertEqual(push_fcm.topics_of(self.db, 1), ["ar-abcdef123456xy"])
        self.assertFalse(push_fcm.pair(self.db, 2, "abcdef123456xy")[0], "код чужого акаунта")
        for i in range(4):
            self.assertTrue(push_fcm.pair(self.db, 1, f"device{i:010d}")[0])
        self.assertFalse(push_fcm.pair(self.db, 1, "overflow123456")[0], "максимум 5 пристроїв")
        self.assertEqual(push_fcm.unpair_all(self.db, 1), 5)

    def test_message_levels(self):
        alarm = push_fcm.build_message("u_x", "🚨", "тривога", "alarm")["message"]
        self.assertEqual(alarm["android"]["notification"]["channel_id"], "airradar_alarm")
        self.assertEqual(alarm["apns"]["payload"]["aps"]["sound"], "alarm.caf")
        info = push_fcm.build_message("u_x", "i", "b", "info")["message"]
        self.assertEqual(info["android"]["notification"]["sound"], "default")

    def test_noop_without_config_and_send_with_config(self):
        push_fcm.pair(self.db, 1, "abcdef123456xy")
        with mock.patch.dict(os.environ, {"FCM_SERVICE_ACCOUNT": "", "NTFY": "0"}):
            self.assertFalse(push_fcm.configured())
            self.assertEqual(asyncio.run(push_fcm.push_user(self.db, 1, "t", "b", "alarm")), 0)
        sent = []
        async def fake_ntfy(payload): sent.append(payload); return 200, "{}"
        with mock.patch.dict(os.environ, {"NTFY": "1", "FCM_SERVICE_ACCOUNT": ""}), mock.patch.object(push_fcm, "_post_ntfy", fake_ntfy):
            self.assertEqual(asyncio.run(push_fcm.push_user(self.db, 1, "t", "b", "alarm")), 1)
            self.db.subscribe(1, "kyivska")
            self.assertEqual(asyncio.run(push_fcm.push_regions(self.db, ["kyivska", "kyivska"], "t", "b")), 1, "унікально")
        self.assertEqual(sent[0]["topic"], "ar-abcdef123456xy")
        # FCM-тема (u_…) працює лише з сервісним ключем
        fcm = []
        async def fake_post(msg): fcm.append(msg); return 200, "{}"
        with mock.patch.dict(os.environ, {"FCM_SERVICE_ACCOUNT": self.sa.name, "NTFY": "0"}), mock.patch.object(push_fcm, "_post", fake_post):
            self.assertTrue(asyncio.run(push_fcm.send_topic("u_xyz", "t", "b", "alarm")))
        self.assertEqual(fcm[0]["message"]["topic"], "u_xyz")


class NtfyTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def test_topic_is_stable_and_push_goes_to_ntfy(self):
        t1 = push_fcm.ntfy_topic_for(self.db, 7)
        self.assertTrue(t1.startswith("ar-") and len(t1) == 27)
        self.assertEqual(push_fcm.ntfy_topic_for(self.db, 7), t1)
        self.assertNotEqual(push_fcm.ntfy_topic_for(self.db, 8), t1)
        sent = []
        async def fake(payload): sent.append(payload); return 200, "{}"
        with mock.patch.dict(os.environ, {"NTFY": "1", "FCM_SERVICE_ACCOUNT": ""}), mock.patch.object(push_fcm, "_post_ntfy", fake):
            self.assertEqual(asyncio.run(push_fcm.push_user(self.db, 7, "🚨 Тривога", "Київ", "alarm")), 1)
        self.assertEqual((sent[0]["topic"], sent[0]["priority"]), (t1, 5))
        self.assertIn("rotating_light", sent[0]["tags"])


class DangerLevelTests(unittest.TestCase):
    def test_danger_is_loud_and_tagged_for_its_own_channel(self):
        n = push_fcm.build_ntfy("ar-x", "t", "b", "danger")
        self.assertEqual(n["priority"], 5)
        self.assertIn("danger", n["tags"])
        self.assertNotIn("danger", push_fcm.build_ntfy("ar-x", "t", "b", "alarm")["tags"])
        m = push_fcm.build_message("u_x", "t", "b", "danger")["message"]
        self.assertEqual(m["android"]["notification"]["channel_id"], "airradar_danger")


if __name__ == "__main__":
    unittest.main()
