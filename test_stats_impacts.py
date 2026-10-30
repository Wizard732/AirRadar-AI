"""Статистика сигналов и журнал наслідків (с задержкой публикации)."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from database import Database
import impact_log
from stats_engine import stats_payload
from threat_parser import parse_post


class StatsImpactTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close()
        self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _event(self, ts, weapon, region, stage="movement", n=[0]):
        n[0] += 1
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO threat_events(event_ts, ingested_ts, weapon_class, stage, region, direction, outcome, confidence, source, fingerprint, text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ts, ts, weapon, stage, region, "", "", 0.8, "s", f"fp{n[0]}", "t"))
            self.db._conn.commit()

    def test_signals_dedupe_across_sources_and_window(self):
        now = int(time.time())
        for k in range(5):  # 5 постов про тот же вылет в одно 30-мин окно
            self._event(now - 60 - k, "shahed", "kharkivska")
        self._event(now - 60, "kab", "kharkivska")
        self._event(now - 60, "uav", "unknown")      # без области — не считаем
        self._event(now - 60, "uav", "sumska", stage="all_clear")  # отбой — не угроза
        st = stats_payload(self.db, now)
        self.assertEqual(st["periods"]["7d"]["drone"], 1)
        self.assertEqual(st["periods"]["7d"]["kab"], 1)
        self.assertEqual(st["periods"]["7d"]["total"], 2)
        self.assertIn("kharkivska", st["active_regions"])
        self.assertEqual(len(st["days"]), 14)
        self.assertEqual(sum(st["hours"]), 2)

    def test_impact_recorded_from_any_channel_with_place_and_coords(self):
        text = "Харків: внаслідок атаки загинули 2 людини, 5 поранені. Пошкоджено житлові будинки."
        parsed = parse_post(text, "x")
        now = int(time.time())
        rec = impact_log.record_from_post(self.db, text, "any_ch", now - 60, parsed)
        self.assertEqual((rec["killed"], rec["injured"], rec["kind"]), (2, 5, "hit"))
        self.assertEqual(rec["place"], "Харків")
        self.assertIsNotNone(rec["lat"])
        self.assertIsNone(impact_log.record_from_post(self.db, text, "other_ch", now - 30, parsed), "дубль в 30-минутном окне не пишем")
        self.assertIn("ВЛУЧАННЯ — ХАРКІВ", impact_log.impact_post_text(rec))
        pay = impact_log.impacts_payload(self.db, now)
        self.assertEqual(len(pay["items"]), 1)
        self.assertEqual((pay["totals"]["killed"], pay["totals"]["injured"], pay["totals"]["hits"]), (2, 5, 1))
        self.assertNotIn("text", pay["items"][0])

    def test_allowed_sources_filter_is_optional(self):
        text = "Приліт у Сумах, пожежа на підприємстві"
        parsed = parse_post(text, "x"); now = int(time.time())
        self.assertIsNone(impact_log.record_from_post(self.db, text, "rnd", now, parsed, frozenset({"official"})))
        self.assertIsNotNone(impact_log.record_from_post(self.db, text, "official", now, parsed, frozenset({"official"})))

    def test_oblast_only_channel_post_and_blast_kind(self):
        now = int(time.time())
        rec = impact_log.record_from_post(self.db, "Вибух, впала.", "sumyliketop", now, parse_post("Вибух, впала.", "sumyliketop"))
        self.assertIsNotNone(rec); self.assertEqual(rec["oblast"], "sumska"); self.assertEqual(rec["precision"], "oblast")
        blast = impact_log.record_from_post(self.db, "Чутко вибухи у Києві", "x", now, parse_post("Чутко вибухи у Києві", "x"))
        self.assertEqual(blast["kind"], "blast")

    def test_noise_is_not_an_impact(self):
        self.assertIsNone(impact_log.extract_impact("Збір на бус, залишилося 18 тис грн"))
        self.assertIsNone(impact_log.extract_impact("Прильотів немає, все спокійно"))


if __name__ == "__main__":
    unittest.main()


class CasualtyUpdateTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False); f.close(); self.path = f.name
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _rec(self, text, ts):
        return impact_log.record_from_post(self.db, text, "ch", ts, parse_post(text, "ch"))

    def test_update_instead_of_duplicate_and_children(self):
        now = int(time.time())
        first = self._rec("Харків: приліт, є загиблі та постраждалі", now - 1800)
        self.assertEqual(first["flags"], "ki")
        upd = self._rec("Харків: загинули 3 людини, 7 поранені, серед них 2 дитини", now - 600)
        self.assertTrue(upd["updated"])
        self.assertEqual((upd["killed"], upd["injured"], upd["children"], upd["flags"]), (3, 7, 2, ""))
        self.assertIsNone(self._rec("Харків: загинули 3 людини, 7 поранені, серед них 2 дитини", now - 300), "те саме — дубль")
        pay = impact_log.impacts_payload(self.db, now)
        self.assertEqual(len(pay["items"]), 1)
        self.assertEqual(pay["totals"]["children"], 2)
        text = impact_log.impact_post_text(upd)
        self.assertIn("ОНОВЛЕННЯ", text); self.assertIn("Загинули: 3", text); self.assertIn("дітей: 2", text)

    def test_unknown_counts_are_not_summed(self):
        now = int(time.time())
        self._rec("Суми: приліт, є загиблі", now - 100)
        pay = impact_log.impacts_payload(self.db, now)
        self.assertEqual((pay["totals"]["killed"], pay["totals"]["unclear"]), (0, 1))
