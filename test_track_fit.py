"""Швидкість і курс за регресією, конус прогнозу, ETA-діапазони (синтетичні траси з шумом геокодингу)."""
import math
import random

from track_engine import Track, Point, build_tracks, destination, forecast_places, haversine_km, bearing_deg

NOW = 1_800_000_000


def _rows(weapon="shahed", profile="", start=(49.0, 31.0), heading=45.0, kmh=180.0, n=5, step_s=360, noise_km=2.0, seed=1):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        lat, lon = destination(start, heading, kmh * (i * step_s) / 3600)
        lat, lon = destination((lat, lon), rnd.uniform(0, 360), rnd.uniform(0, noise_km))
        rows.append({"event_ts": NOW - (n - 1 - i) * step_s, "as_of": "", "source": "s", "weapon": weapon, "profile": profile,
                     "count": 1, "group_count": None, "oblast": "", "place": f"p{i}", "relation": "at",
                     "lat": lat, "lon": lon, "precision": "place", "heading_from": ""})
    return rows


def _one(rows):
    ts = build_tracks(rows, NOW)
    assert len(ts) == 1
    return ts[0]


def test_regression_gives_stable_heading_and_speed_under_noise():
    for seed in range(8):
        t = _one(_rows(seed=seed))
        assert t.heading_source == "fit" and t.speed_source == "measured"
        assert min(abs(t.heading - 45) % 360, 360 - abs(t.heading - 45) % 360) < 12, (seed, t.heading)
        assert abs(t.speed_kmh - 180) / 180 < 0.25, (seed, t.speed_kmh)


def test_two_points_fall_back_to_segment_with_wider_sigma():
    t = _one(_rows(n=2, noise_km=0.0))
    assert t.heading_source == "segments" and t.sigma >= 12


def test_noisy_track_has_larger_sigma_than_clean_one():
    clean, noisy = _one(_rows(noise_km=0.3)), _one(_rows(noise_km=6.0, seed=3))
    assert noisy.sigma > clean.sigma and clean.sigma <= 12


def test_reactive_speed_is_measured_in_its_own_corridor():
    t = _one(_rows(weapon="uav", profile="reactive", kmh=550, step_s=240, noise_km=1.5))
    assert t.speed_source == "measured" and 400 <= t.speed_kmh <= 700


def test_forecast_cone_widens_and_eta_is_a_range(monkeypatch):
    import track_engine as te

    class Town:
        def __init__(self, name, lat, lon): self.name, self.lat, self.lon = name, lat, lon
    t = Track(id="x", weapon="shahed", profile="", heading=0.0, speed_kmh=180.0, speed_source="measured", sigma=10.0)
    t.points = [Point(NOW, 50.0, 30.0, 1, "p", "place")]
    near_axis = Town("OnAxis", *destination((50.0, 30.0), 0, 60))
    # 60 км уперед і ~11 км убік: поза вузьким коридором 12 км? ні — всередині конуса на цій відстані
    side = Town("Side", *destination(destination((50.0, 30.0), 0, 90), 90, 18))
    far_side = Town("FarSide", *destination(destination((50.0, 30.0), 0, 20), 90, 18))
    monkeypatch.setattr(te.gazetteer, "towns", lambda pop: [near_axis, side, far_side])
    got = {p.name: p for p in forecast_places(t, NOW)}
    assert "OnAxis" in got and got["OnAxis"].conf == "висока" and got["OnAxis"].eta_lo < got["OnAxis"].eta_min < got["OnAxis"].eta_hi
    assert "Side" in got                      # на 90 км конус ширший за базові 12 км
    assert "FarSide" not in got               # на 20 км убік 18 км — поза конусом
    assert abs(got["OnAxis"].eta_min - 20) <= 1


def test_ballistic_horizon_is_short(monkeypatch):
    import track_engine as te

    class Town:
        def __init__(self, name, lat, lon): self.name, self.lat, self.lon = name, lat, lon
    t = Track(id="b", weapon="ballistic", profile="", heading=0.0, speed_kmh=2000.0, speed_source="typical", sigma=10.0)
    t.points = [Point(NOW, 50.0, 30.0, 1, "p", "place")]
    monkeypatch.setattr(te.gazetteer, "towns", lambda pop: [Town("A", *destination((50.0, 30.0), 0, 400)), Town("B", *destination((50.0, 30.0), 0, 900))])
    names = [p.name for p in forecast_places(t, NOW)]
    assert names == ["A"]   # 15 хв × 2000 км/год = 500 км; 900 км — поза горизонтом
