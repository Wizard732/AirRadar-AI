"""Тесты быстрых побед: ETA в постах, ночной режим, фидбек, метрика сирены."""

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from database import Database
from dedup import DedupCache
from incident_fusion import extract_incident_fact
from alert_renderer import render_evidence_alert

main = None


def _import_main():
    global main
    if main is None:
        import main as _main
        main = _main
    return main


class FakeSummarizer:
    async def summarize(self, text: str, *a, **kw) -> str:
        return text

    async def classify(self, text: str, prompt: str, max_tokens: int = 30) -> str:
        return ""


class FakeBotClient:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, user_id, text, link_preview=False, buttons=None):
        self.sent.append((user_id, text, buttons))


class EtaInPostTests(unittest.TestCase):
    """ETA «орієнтовно» появляется в посте с известным классом оружия."""

    def _render(self, weapon: str) -> str:
        fact = extract_incident_fact("БпЛА курсом на Київ", weapon, "imminent")
        return render_evidence_alert(
            text="БпЛА курсом на Київ", source="test", event_ts=int(time.time()),
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )

    def test_uav_post_has_eta_line(self):
        post = self._render("uav")
        self.assertIn("Типовий підліт", post)
        self.assertIn("орієнтовно", post)

    def test_stand_down_has_no_eta(self):
        fact = extract_incident_fact("Відбій тривоги у Києві", "stand_down", "unknown")
        post = render_evidence_alert(
            text="Відбій тривоги у Києві", source="test", event_ts=int(time.time()),
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertNotIn("Типовий підліт", post)


class NightModeTests(unittest.TestCase):
    """Ночной режим: персональная настройка + фильтрация некритичных ночью."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_prefs_roundtrip(self):
        self.assertFalse(self.db.get_night_mode(1))
        self.db.set_night_mode(1, True)
        self.assertTrue(self.db.get_night_mode(1))
        self.db.set_night_mode(1, False)
        self.assertFalse(self.db.get_night_mode(1))

    def test_night_filters_noncritical_for_opted_in(self):
        """Ночь: подписчик с ночным режимом НЕ получает некритичный БПЛА-алерт."""
        self.db.subscribe(1, "odeska")
        self.db.set_night_mode(1, True)
        bot = FakeBotClient()
        # Мокаем ночное время.
        orig = main._is_night_time
        main._is_night_time = lambda: True
        try:
            asyncio.run(main._notify_subscribers(
                bot, self.db, ["odeska"], "🟡 ОДЕСА | БПЛА\nтест",
                weapon_class="uav",
            ))
        finally:
            main._is_night_time = orig
        self.assertEqual(bot.sent, [])

    def test_night_passes_critical_and_stand_down(self):
        """Ночь: балістика и відбій доставляются даже с ночным режимом."""
        self.db.subscribe(1, "odeska")
        self.db.set_night_mode(1, True)
        bot = FakeBotClient()
        orig = main._is_night_time
        main._is_night_time = lambda: True
        try:
            asyncio.run(main._notify_subscribers(
                bot, self.db, ["odeska"], "🔴 ОДЕСА | Балістика\nтест",
                weapon_class="ballistic",
            ))
            self.assertEqual(len(bot.sent), 1)
            bot.sent.clear()
            asyncio.run(main._notify_subscribers(
                bot, self.db, ["odeska"], "🟢 ВІДБІЙ — ОДЕСА — 03:00",
                weapon_class="stand_down",
            ))
            self.assertEqual(len(bot.sent), 1)
        finally:
            main._is_night_time = orig

    def test_day_delivers_everything(self):
        """День (или выключенный режим): доставка без фильтра."""
        self.db.subscribe(1, "odeska")
        self.db.set_night_mode(1, True)
        bot = FakeBotClient()
        orig = main._is_night_time
        main._is_night_time = lambda: False
        try:
            asyncio.run(main._notify_subscribers(
                bot, self.db, ["odeska"], "🟡 ОДЕСА | БПЛА\nтест",
                weapon_class="uav",
            ))
        finally:
            main._is_night_time = orig
        self.assertEqual(len(bot.sent), 1)

    def test_feedback_buttons_attached(self):
        """С feedback_key подписка приходит с кнопками качества."""
        self.db.subscribe(1, "odeska")
        bot = FakeBotClient()
        asyncio.run(main._notify_subscribers(
            bot, self.db, ["odeska"], "🟡 ОДЕСА | БПЛА\nтест",
            feedback_key="k1", weapon_class="uav",
        ))
        self.assertEqual(len(bot.sent), 1)
        _uid, _text, buttons = bot.sent[0]
        self.assertIsNotNone(buttons)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_feedback_counters(self):
        self.assertEqual(self.db.get_alert_feedback("k1"), {"useful": 0, "noise": 0, "error": 0})
        self.db.add_alert_feedback("k1", "useful")
        self.db.add_alert_feedback("k1", "useful")
        self.db.add_alert_feedback("k1", "noise")
        self.assertEqual(self.db.get_alert_feedback("k1"), {"useful": 2, "noise": 1, "error": 0})

    def test_feedback_invalid_vote_ignored(self):
        self.db.add_alert_feedback("k1", "wat")
        self.assertEqual(self.db.get_alert_feedback("k1"), {"useful": 0, "noise": 0, "error": 0})

    def test_feedback_error_vote(self):
        """Кнопка «❌ Помилка»: третий голос фиксирует ошибочный пост."""
        self.db.add_alert_feedback("k1", "error")
        self.assertEqual(self.db.get_alert_feedback("k1"), {"useful": 0, "noise": 0, "error": 1})


class SirenLeadTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_record_and_stats(self):
        now = int(time.time())
        # Эпизод 1: мы на 7 минут раньше.
        self.assertTrue(self.db.record_siren_lead("kyivska", now - 420, now))
        # Эпизод 2: мы на 2 минуты позже сирены.
        self.db.record_siren_lead("odeska", now + 120, now)
        # Дубликат той же тревоги игнорируется.
        self.assertFalse(self.db.record_siren_lead("kyivska", now - 100, now))
        stats = self.db.siren_lead_stats(days=7)
        self.assertEqual(stats["episodes"], 2)
        self.assertEqual(stats["before_count"], 1)
        self.assertAlmostEqual(stats["avg_lead_sec"], 420.0)

    def test_stats_empty(self):
        stats = self.db.siren_lead_stats(days=7)
        self.assertEqual(stats, {"episodes": 0, "avg_lead_sec": 0.0, "before_count": 0})


if __name__ == "__main__":
    unittest.main()
