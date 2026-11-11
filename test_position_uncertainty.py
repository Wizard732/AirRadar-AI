"""Положение цели: неточные точки помечаются, экстраполяция ограничена и не обгоняет пункт назначения."""
from track_engine import EXTRAPOLATE_MAX_S, Point, Track, haversine_km, position_at, position_radius_km


def _track(precision="place", dest=None, heading=0.0):
    t = Track(id="t", weapon="shahed", profile="", points=[Point(1000, 50.0, 30.0, 1, "X", precision)],
              speed_kmh=180.0, heading=heading, status="active")
    t.dest = dest
    return t


def test_extrapolation_capped():
    t = _track()
    lat, lon, est = position_at(t, 1000 + 3600)
    assert est
    assert haversine_km((50.0, 30.0), (lat, lon)) <= 180.0 * EXTRAPOLATE_MAX_S / 3600 + 0.5


def test_extrapolation_does_not_pass_destination():
    t = _track(dest=(50.1, 30.0, "Y"))
    lat, lon, _ = position_at(t, 1000 + EXTRAPOLATE_MAX_S)
    assert haversine_km((50.0, 30.0), (lat, lon)) <= haversine_km((50.0, 30.0), (50.1, 30.0)) + 0.5


def test_approx_point_is_estimated_with_radius():
    t = _track(precision="approx", heading=None)
    assert position_at(t, 1000)[2] is True
    assert position_radius_km(t, 1000) > 0


def test_exact_point_has_no_radius():
    t = _track(heading=None)
    assert position_at(t, 1000)[2] is False
    assert position_radius_km(t, 1000) == 0.0
