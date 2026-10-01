"""Тесты единого киевского времени (kyiv_time.py).

Контекст: сервер живёт в UTC, и до фикса все посты показывали «американское»
время (time.localtime в контейнере == UTC). Теперь все пользовательские строки
форматируются через kyiv_time (ручной DST: последнее воскресенье марта/октября).
"""

from __future__ import annotations

import calendar
import unittest

from kyiv_time import fmt, kyiv_date, kyiv_hour, kyiv_midnight_ts


def ts_utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> int:
    """Unix-ts для UTC-момента (независимо от TZ машины, где гоняются тесты)."""
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


class KyivTimeTests(unittest.TestCase):
    def test_summer_is_eest_plus3(self):
        """1 октября 2026 — лето (переход 25 октября): 06:51 UTC → 09:51 Kyiv."""
        self.assertEqual(fmt(ts_utc(2026, 10, 1, 6, 51)), "09:51")

    def test_winter_is_eet_plus2(self):
        """1 декабря 2026 — зима: 10:00 UTC → 12:00 Kyiv."""
        self.assertEqual(fmt(ts_utc(2026, 12, 1, 10)), "12:00")

    def test_dst_boundary_march(self):
        """Переход на лето: последнее воскресенье марта 2026 = 29-е.

        00:59 UTC = 02:59 EET (+2), 01:00 UTC = 04:00 EEST (+3).
        """
        self.assertEqual(fmt(ts_utc(2026, 3, 29, 0, 59)), "02:59")
        self.assertEqual(fmt(ts_utc(2026, 3, 29, 1, 0)), "04:00")

    def test_dst_boundary_october(self):
        """Переход на зиму: последнее воскресенье октября 2026 = 25-е.

        00:59 UTC = 03:59 EEST (+3), 01:00 UTC = 03:00 EET (+2).
        """
        self.assertEqual(fmt(ts_utc(2026, 10, 25, 0, 59)), "03:59")
        self.assertEqual(fmt(ts_utc(2026, 10, 25, 1, 0)), "03:00")

    def test_hour_and_date_helpers(self):
        ts = ts_utc(2026, 10, 1, 21, 30)
        self.assertEqual(kyiv_hour(ts), 0)  # 00:30 следующего дня
        self.assertEqual(kyiv_date(ts), "2026-10-02")  # дата уже следующая по Киеву

    def test_midnight_ts_matches_kyiv_day_start(self):
        """Полночь Киева 01.10 = 21:00 UTC 30.09 (лето)."""
        expect = ts_utc(2026, 9, 30, 21)
        self.assertEqual(kyiv_midnight_ts(ts_utc(2026, 10, 1, 6, 30)), expect)
        # Зимой полночь Киева = 22:00 UTC предыдущего дня.
        expect_w = ts_utc(2026, 11, 30, 22)
        self.assertEqual(kyiv_midnight_ts(ts_utc(2026, 12, 1, 5)), expect_w)

    def test_alert_header_shows_kyiv_time(self):
        """Шапка поста показывает киевское время, а не локальное машины/сервера."""
        from alert_renderer import render_evidence_alert
        from incident_fusion import IncidentFact

        ts = ts_utc(2026, 10, 1, 6, 51)  # юзерский кейс: было «06:51», надо «09:51»
        fact = IncidentFact("uav", "imminent", "", "kyivska", "exact", None, False, "")
        post = render_evidence_alert(
            text="т", source="s", event_ts=ts,
            fact=fact, confirmation={"status": "corroborated", "key": "k"},
        )
        self.assertIn("🕒 09:51", post)


if __name__ == "__main__":
    unittest.main()
