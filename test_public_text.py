"""test_public_text.py — правило №6: имена каналов/ссылки не попадают в вывод.

Покрывает:
- fast_filter.public_text: срез markdown/t.me-ссылок, служебных префиксов,
  декора каналов, обрезка по границе слова с «…»;
- /api/threats (health_server): текст события приходит уже публичным;
- merge_incident_fact(official=True) → официально подтверждённый инцидент;
- regions.region_name для служебных slug-ов multi/unknown.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest

from fast_filter import public_text


class PublicTextTests(unittest.TestCase):
    """Санитайзер публичного текста (тикер карты, тексты бота)."""

    def test_strips_multi_prefix_and_channel_markdown_link(self):
        """Кейс со скриншота: 'multi: [Сумщина](https://t.me/+...): БПЛА…'."""
        raw = (
            "multi: [Сумщина](https://t.me/+CvyxFii9afI5MDgy): "
            "БПЛА на Терни Бпла вночі"
        )
        out = public_text(raw, 60)
        self.assertNotIn("multi:", out.lower())
        self.assertNotIn("t.me", out)
        self.assertNotIn("](", out)
        self.assertIn("БПЛА на Терни", out)

    def test_truncates_on_word_boundary_with_ellipsis(self):
        raw = "БПЛА на Терни Бпла вночі, курс на південь області підтверджено"
        out = public_text(raw, 25)
        self.assertTrue(out.endswith("…"))
        # «Б» без хвоста не остаётся: режем по слову.
        self.assertFalse(out.rstrip("…").endswith(" Терни Б"))
        self.assertLessEqual(len(out), 26)

    def test_strips_bold_markdown_and_newlines(self):
        raw = "**Чернігівщина:**\nРеактивний БпЛА курсом на Понорницю"
        out = public_text(raw, 90)
        self.assertNotIn("**", out)
        self.assertNotIn("\n", out)
        self.assertIn("Понорницю", out)

    def test_strips_bare_urls(self):
        out = public_text("Вибух у Дарницькому районі https://t.me/abc", 90)
        self.assertNotIn("t.me", out)
        self.assertIn("Вибух", out)

    def test_channel_prefix_signature_keeps_content(self):
        """Канал-префикс с маркерным именем не должен съедать контент."""
        raw = "✙ Розвідка неба ✙ БпЛА на Київ ✙[ Розвідка неба ](https://t.me/rozvidkaneba)✙"
        out = public_text(raw, 60)
        self.assertIn("БпЛА на Київ", out)
        self.assertNotIn("rozvidkaneba", out)
        self.assertNotIn("✙", out)

    def test_empty_and_short_inputs(self):
        self.assertEqual(public_text("", 60), "")
        self.assertEqual(public_text("Коротко", 90), "Коротко")

    def test_long_single_word_hard_cut(self):
        out = public_text("Слово" * 30, 20)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 21)


class ApiThreatsPublicTextTests(unittest.TestCase):
    """GET /api/threats: текст события не содержит ссылок/служебных префиксов."""

    def setUp(self):
        from database import Database
        from incident_fusion import extract_incident_fact
        from health_server import set_db

        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        raw = "multi: [Сумщина](https://t.me/+CvyxFii9afI5MDgy): БПЛА на Терни Бпла"
        fact = extract_incident_fact(raw, "uav", "imminent")
        self.db.merge_incident_fact(
            event_ts=int(time.time()), source="s", source_group="s",
            fact=fact, text=raw,
        )
        set_db(self.db)

    def tearDown(self):
        from health_server import set_db
        set_db(None)
        self.db.close()
        os.unlink(self.file.name)

    def test_api_text_is_public(self):
        from health_server import _api_threats

        response = asyncio.run(_api_threats(None))
        data = json.loads(response.text)
        self.assertTrue(data["threats"])
        text = data["threats"][0]["text"]
        self.assertNotIn("t.me", text)
        self.assertNotIn("](", text)
        self.assertNotIn("multi:", text.lower())
        self.assertIn("БПЛА на Терни", text)


class MergeOfficialTests(unittest.TestCase):
    """merge_incident_fact(official=True) → официально подтверждённый статус."""

    def setUp(self):
        from database import Database

        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_official_post_confirms_incident(self):
        from incident_fusion import extract_incident_fact

        fact = extract_incident_fact("БпЛА на Київ", "uav", "imminent")
        result = self.db.merge_incident_fact(
            event_ts=int(time.time()), source="dsns", source_group="dsns",
            fact=fact, text="БпЛА на Київ", official=True,
        )
        self.assertEqual(result["status"], "officially_confirmed")

    def test_official_followup_upgrades_existing_incident(self):
        from incident_fusion import extract_incident_fact

        fact = extract_incident_fact("БпЛА на Київ", "uav", "imminent")
        first = self.db.merge_incident_fact(
            event_ts=int(time.time()), source="a", source_group="a",
            fact=fact, text="БпЛА на Київ",
        )
        self.assertEqual(first["status"], "reported")
        second = self.db.merge_incident_fact(
            event_ts=int(time.time()) + 10, source="dsns", source_group="dsns",
            fact=fact, text="БпЛА на Київ", official=True,
        )
        self.assertTrue(second["merged"])
        self.assertEqual(second["status"], "officially_confirmed")

    def test_without_official_flag_behaves_as_before(self):
        from incident_fusion import extract_incident_fact

        fact = extract_incident_fact("БпЛА на Київ", "uav", "imminent")
        result = self.db.merge_incident_fact(
            event_ts=int(time.time()), source="a", source_group="a",
            fact=fact, text="БпЛА на Київ",
        )
        self.assertEqual(result["status"], "reported")


class RegionNameSpecialTests(unittest.TestCase):
    """Служебные slug-и инцидентов человекочитаемы, а не сырым 'multi:'."""

    def test_multi_and_unknown(self):
        from regions import region_name

        self.assertEqual(region_name("multi"), "Кілька областей")
        self.assertEqual(region_name("unknown"), "Регіон уточнюється")
        # kyivska — «Київ та область» в REGIONS (не падает на fallback).
        self.assertNotEqual(region_name("kyivska"), "kyivska")


class PublicAccuracyTests(unittest.TestCase):
    """wave_forecast.public_accuracy: агрегированный hit-rate (leave-one-out)."""

    def setUp(self):
        from database import Database

        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_empty_db_is_unavailable(self):
        from wave_forecast import public_accuracy

        acc = public_accuracy(self.db)
        self.assertFalse(acc["available"])
        self.assertEqual(acc["total"], 0)


if __name__ == "__main__":
    unittest.main()
