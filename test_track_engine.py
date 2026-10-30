import unittest
from datetime import datetime, timezone

from track_engine import (
    build_tracks, bearing_deg, compass_ua, destination, forecast_places, haversine_km,
    obs_timestamp, position_at, risk_for_point, risk_text,
)

T0 = int(datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc).timestamp())


def row(ts, lat, lon, weapon="shahed", count=1, place="X", profile="", as_of="", relation="at"):
    return {"event_ts": ts, "as_of": as_of, "source": "kiev_nebo", "weapon": weapon, "profile": profile,
            "count": count, "group_count": None, "oblast": "", "place": place, "relation": relation,
            "lat": lat, "lon": lon, "precision": "place", "heading_from": ""}


def northbound_track():
    """Цель идёт на север со скоростью ~180 км/ч: за 10 минут — 30 км."""
    p0 = (49.0, 31.0)
    p1 = destination(p0, 0, 30)
    p2 = destination(p0, 0, 60)
    return [row(T0, *p0, place="A"), row(T0 + 600, *p1, place="B"), row(T0 + 1200, *p2, place="C")]


class GeometryTests(unittest.TestCase):
    def test_bearing_and_distance(self):
        self.assertAlmostEqual(bearing_deg((49, 31), (50, 31)), 0, delta=0.5)
        self.assertAlmostEqual(haversine_km((49, 31), (50, 31)), 111.2, delta=0.5)
        self.assertEqual(compass_ua(45), "північний схід")


class TimeTests(unittest.TestCase):
    def test_as_of_uses_kyiv_clock(self):
        # Октябрь, после перехода на зимнее время Киев = UTC+2: 21:05 UTC = 23:05 Киев.
        # «станом на 23:00» — на 5 минут раньше публикации.
        ev = T0 + 3600 + 300
        self.assertEqual(obs_timestamp({"event_ts": ev, "as_of": "23:00"}), T0 + 3600)

    def test_as_of_after_midnight_goes_to_previous_day(self):
        ev = int(datetime(2026, 10, 28, 22, 5, tzinfo=timezone.utc).timestamp())  # 00:05 Киев 29-го
        self.assertEqual(obs_timestamp({"event_ts": ev, "as_of": "23:50"}), ev - 15 * 60)


class TrackTests(unittest.TestCase):
    def test_speed_and_heading_come_from_displacement(self):
        tracks = build_tracks(northbound_track(), T0 + 1260)
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t.speed_source, "measured")
        self.assertAlmostEqual(t.speed_kmh, 180, delta=10)
        self.assertAlmostEqual(t.heading, 0, delta=3)
        self.assertEqual(t.status, "active")

    def test_single_point_has_no_heading_and_typical_speed(self):
        t = build_tracks([row(T0, 49, 31)], T0 + 60)[0]
        self.assertIsNone(t.heading)
        self.assertEqual(t.speed_source, "typical")

    def test_unreachable_jump_starts_new_track(self):
        rows = [row(T0, 49, 31), row(T0 + 300, 52, 31)]  # 330 км за 5 минут
        self.assertEqual(len(build_tracks(rows, T0 + 400)), 2)

    def test_reactive_gets_reactive_speed(self):
        p0 = (49.0, 31.0)
        rows = [row(T0, *p0, weapon="uav", profile="reactive"),
                row(T0 + 600, *destination(p0, 90, 95), weapon="uav", profile="reactive")]
        t = build_tracks(rows, T0 + 660)[0]
        self.assertAlmostEqual(t.speed_kmh, 570, delta=30)

    def test_stale_track_is_not_extrapolated(self):
        t = build_tracks(northbound_track(), T0 + 1200 + 3600)[0]
        self.assertEqual(t.status, "stale")
        lat, lon, est = position_at(t, T0 + 1200 + 3600)
        self.assertFalse(est)

    def test_position_is_extrapolated_along_heading(self):
        t = build_tracks(northbound_track(), T0 + 1200 + 300)[0]
        lat, lon, est = position_at(t, T0 + 1200 + 300)
        self.assertTrue(est)
        self.assertAlmostEqual(haversine_km((lat, lon), destination((49, 31), 0, 60)), 15, delta=2)

    def test_interpolation_inside_track(self):
        t = build_tracks(northbound_track(), T0 + 1260)[0]
        lat, lon, est = position_at(t, T0 + 300)
        self.assertFalse(est)
        self.assertAlmostEqual(haversine_km((lat, lon), (49, 31)), 15, delta=1)

    def test_forecast_lists_towns_on_course(self):
        t = build_tracks(northbound_track(), T0 + 1260)[0]
        places = forecast_places(t, T0 + 1260, horizon_min=90, min_population=3000)
        self.assertTrue(places)
        self.assertEqual(places, sorted(places, key=lambda p: p.eta_min))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tracks = build_tracks(northbound_track(), T0 + 1200)
        self.head = destination((49, 31), 0, 60)  # текущая позиция цели

    def test_target_ahead_on_course_is_imminent_with_eta(self):
        user = destination(self.head, 0, 8)
        risks = risk_for_point(self.tracks, user, T0 + 1200)
        self.assertEqual(risks[0].level, "imminent")
        self.assertTrue(risks[0].approaching)
        self.assertAlmostEqual(risks[0].eta_min, 3, delta=1)

    def test_target_flying_away_is_not_a_risk_beyond_over_radius(self):
        user = destination(self.head, 180, 8)  # позади цели
        self.assertEqual(risk_for_point(self.tracks, user, T0 + 1200), [])

    def test_side_pass_inside_cpa_is_watch(self):
        user = destination(destination(self.head, 0, 20), 90, 6)  # впереди 20 км, сбоку 6 км
        risks = risk_for_point(self.tracks, user, T0 + 1200)
        self.assertEqual(risks[0].level, "watch")

    def test_far_target_is_ignored(self):
        user = destination(self.head, 0, 80)
        self.assertEqual(risk_for_point(self.tracks, user, T0 + 1200), [])

    def test_over_you(self):
        risks = risk_for_point(self.tracks, destination(self.head, 0, 1.5), T0 + 1200)
        self.assertEqual(risks[0].level, "over")
        self.assertIn("над вами", risk_text(risks[0]))

    def test_text_is_one_line_and_hedged(self):
        r = risk_for_point(self.tracks, destination(self.head, 0, 8), T0 + 1200)[0]
        text = risk_text(r)
        self.assertNotIn("\n", text)
        self.assertIn("км", text)
        self.assertIn("орієнтовно", text)


if __name__ == "__main__":
    unittest.main()
