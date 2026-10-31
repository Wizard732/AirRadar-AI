"""Тести публічного подання (airradar/safety/public_view.py): жодних live-координат, швидкості, курсу, прогнозу."""
import json
import re

from airradar.safety import public_view as pv

NOW = 1_700_000_000
# Синтетика: точки всередині реальних районів (Дніпровський район Дніпропетровської; Березівський район Одеської).
DNIPRO = (48.60, 35.30)
ODESA = (47.20, 30.95)


def _private():
    def tr(i, weapon, ll, status="active", sources=2, count=1):
        return {"id": f"t{i}", "weapon": weapon, "status": status, "sources": sources, "count": count,
                "speed_kmh": 180.0, "speed_source": "measured", "heading": 45, "heading_text": "схід",
                "now": {"lat": ll[0] + 0.01234, "lon": ll[1] + 0.04321, "estimated": True},
                "points": [{"ts": NOW - 700 + i, "lat": ll[0], "lon": ll[1], "count": count, "place": "X", "precision": "place"},
                           {"ts": NOW - 100 + i, "lat": ll[0] + 0.01, "lon": ll[1] + 0.01, "count": count, "place": "Y", "precision": "place"}],
                "forecast": [{"name": "Z", "lat": 50.1234, "lon": 30.1234, "eta_min": 20, "km": 25}],
                "dest": {"lat": 50.4501, "lon": 30.5234, "name": "Київ"}}
    return {"now": NOW, "window_min": 120,
            "tracks": [tr(1, "shahed", DNIPRO, count=3), tr(2, "uav", DNIPRO, sources=1), tr(3, "cruise_missile", ODESA, sources=3)],
            "events": [{"weapon": "kab", "lat": 48.601, "lon": 35.301, "ts": NOW - 60, "count": 1, "place": "P", "oblast": "O",
                        "precision": "place", "toward": False}],
            "impacts": [], "sirens": ["dnipropetrovska"], "sirens_raions": None, "sirens_raions_y": None, "sirens_raions_p": None,
            "siren_threats": {"dnipropetrovska": ["drones"]}, "alerts": [], "feed": [], "accuracy": None}


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _floats_with_3_decimals(o):
    txt = json.dumps(o, ensure_ascii=False)
    return re.findall(r"\b\d{1,3}\.\d{3,}\b", txt)


def test_clean_contract_has_no_forbidden_keys():
    c = pv.regions_contract(pv.build_public(_private(), NOW))
    leaked = {k for k, _ in _walk(c["regions"])} & pv.FORBIDDEN_KEYS
    assert not leaked
    assert c["regions"] and c["notice"] and c["schema"] == 1


def test_no_coordinates_with_three_decimals_anywhere_public():
    p = pv.build_public(_private(), NOW)
    assert _floats_with_3_decimals(p) == []


def test_compat_tracks_hide_speed_heading_forecast_dest():
    p = pv.build_public(_private(), NOW)
    assert p["tracks"]
    for t in p["tracks"]:
        assert t["speed_kmh"] is None and t["heading"] is None and t["dest"] is None and t["forecast"] == []
        assert len(t["points"]) == 1 and t["points"][0]["precision"] == "region"


def test_positions_are_fixed_raion_centroids_not_live_points():
    priv = _private()
    a = pv.build_public(priv, NOW)
    for tr in priv["tracks"]:      # зсув live-точки не змінює публічну позицію
        tr["now"]["lat"] += 0.02
    b = pv.build_public(priv, NOW)
    assert {(t["now"]["lat"], t["now"]["lon"]) for t in a["tracks"]} == {(t["now"]["lat"], t["now"]["lon"]) for t in b["tracks"]}


def test_grouping_buckets_and_category_priority():
    p = pv.build_public(_private(), NOW)
    drone = [r for r in p["regions"] if r["category"] == "drone"]
    missile = [r for r in p["regions"] if r["category"] == "missile"]
    assert len(drone) == 1 and drone[0]["count_bucket"] == "2-5" and drone[0]["sources_bucket"] == "2"
    assert len(missile) == 1 and missile[0]["sources_bucket"] == "3+" and missile[0]["status"] == "confirmed"


def test_times_rounded_to_five_minutes():
    p = pv.build_public(_private(), NOW)
    for r in p["regions"]:
        assert r["first_seen"] % 300 == 0 and r["last_update"] % 300 == 0
    assert p["updated_at"] % 300 == 0


def test_key_is_region_and_category_not_target_id():
    """Ключ елемента — район+категорія: стабільний у часі і не залежить від окремої цілі."""
    priv = _private()
    a = {t["id"] for t in pv.build_public(priv, NOW)["tracks"]}
    later = json.loads(json.dumps(priv))
    for t in later["tracks"]:
        t["id"] = "other-" + t["id"]
        for pt in t["points"]:
            pt["ts"] += 600
    b = {t["id"] for t in pv.build_public(later, NOW + 600)["tracks"]}
    assert a == b


def test_no_sub_bucket_side_channel():
    """Зсув live-позицій у межах району і часу в межах 5-хв корзини не змінює публічну відповідь."""
    priv = _private()
    shifted = json.loads(json.dumps(priv))
    for t in shifted["tracks"]:
        t["now"]["lat"] += 0.003
        for pt in t["points"]:
            pt["ts"] = pt["ts"] // 300 * 300 + 150   # інша секунда в межах тієї ж корзини
    base = json.loads(json.dumps(priv))
    for t in base["tracks"]:
        for pt in t["points"]:
            pt["ts"] = pt["ts"] // 300 * 300 + 10
    assert pv.build_public(base, NOW) == pv.build_public(shifted, NOW)


def test_point_outside_raions_is_not_published():
    priv = _private()
    priv["tracks"] = [dict(priv["tracks"][0], now={"lat": 10.0, "lon": 10.0, "estimated": True})]
    priv["events"] = []
    assert pv.build_public(priv, NOW)["tracks"] == []


def test_stale_track_is_marked_stale():
    priv = _private()
    priv["tracks"] = [dict(priv["tracks"][0], status="stale")]
    p = pv.build_public(priv, NOW)
    assert p["regions"][0]["stale"] is True and p["tracks"][0]["status"] == "stale"


def test_strip_threat_removes_vector_fields():
    item = {"ts": 1, "type": "shahed", "region": "kyivska", "text": "t", "track": {"course_deg": 10, "speed_kmh": 150},
            "origin": "sumska", "destination": "kyivska", "age_min": 3}
    out = pv.strip_threat(item)
    assert "track" not in out and "origin" not in out and "destination" not in out and out["region"] == "kyivska"


def test_public_risk_has_no_distance_bearing_eta_speed():
    risks = [{"level": "watch", "distance_km": 12.3, "bearing": 90, "eta_min": 9, "speed_kmh": 180, "text": "x"},
             {"level": "imminent", "distance_km": 4.1, "bearing": 40, "eta_min": 3, "speed_kmh": 180, "text": "y"}]
    out = pv.public_risk(risks)
    assert out == [{"level": "imminent", "text": pv._RISK_TEXT["imminent"]}]  # noqa: SLF001
    assert pv.public_risk([]) == []


def test_category_follows_normalized_type():
    assert pv.category_of("shahed") == "drone" and pv.category_of("ballistic") == "missile"
    assert pv.category_of("kab") == "other" and pv.category_of("???") == "other"


# --- інтеграція з health_server (режим region за замовчуванням) ---------------------------------

import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

import health_server
from database import Database


class PublicApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"PUBLIC_MODE": "region"})
        self._env.start()
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        health_server.set_db(self.db)
        health_server._risk_hits.clear()  # noqa: SLF001
        self.seed()

    def tearDown(self):
        self._env.stop()
        health_server.set_db(None)
        self.db.close()
        os.unlink(self.file.name)

    def seed(self):
        from threat_parser import Observation
        from track_engine import destination
        now = int(time.time())
        p0 = (49.0, 31.0)
        for i, d in enumerate((0, 30, 60)):
            lat, lon = destination(p0, 0, d)
            self.db.add_observations(now - 3000 + i * 600, "kiev_nebo",
                                     [Observation("shahed", "", 2, None, "kyivska", f"P{i}", "at", lat, lon, "place")])
        self.user = destination(p0, 0, 68)

    def get(self, handler, query="", headers=None, path="/x"):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", path + ("?" + query if query else ""), headers=headers or {})
        resp = asyncio.run(handler(req))
        return resp.status, (json.loads(resp.text) if resp.text and resp.text.startswith(("{", "[")) else resp.text)

    def test_region_mode_has_default_delay(self):
        with mock.patch.dict(os.environ, {"PUBLIC_MODE": "region"}):
            os.environ.pop("TRACKS_PUBLIC_DELAY_MIN", None)
            assert health_server.public_mode() == "region" and health_server.public_delay_s() == 600

    def test_default_is_full_data_by_owner_decision(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUBLIC_MODE", None)
            os.environ.pop("TRACKS_PUBLIC_DELAY_MIN", None)
            assert health_server.public_mode() == "legacy" and health_server.public_delay_s() == 0
            status, body = self.get(health_server._api_tracks, "minutes=120")  # noqa: SLF001
        assert status == 200 and body["tracks"] and len(body["tracks"][0]["points"]) == 3

    def test_api_tracks_is_public_no_precise_data(self):
        status, body = self.get(health_server._api_tracks, "minutes=120")  # noqa: SLF001
        assert status == 200 and body["public"] is True and body["notice"]
        assert body["tracks"], "трек із районом має бути"
        for t in body["tracks"]:
            assert t["speed_kmh"] is None and t["heading"] is None and t["dest"] is None and t["forecast"] == []
            assert len(t["points"]) == 1
        assert _floats_with_3_decimals(body) == []

    def test_public_regions_contract_clean(self):
        status, body = self.get(health_server._api_public_regions, "minutes=120")  # noqa: SLF001
        assert status == 200 and body["schema"] == 1 and body["regions"]
        assert not ({k for k, _ in _walk(body["regions"])} & pv.FORBIDDEN_KEYS)

    def test_risk_is_level_only_and_rate_limited(self):
        status, body = self.get(health_server._api_risk, f"lat={self.user[0]}&lon={self.user[1]}&radius=2")  # noqa: SLF001
        assert status == 200
        for r in body["risks"]:
            assert set(r) == {"level", "text"}
        codes = [self.get(health_server._api_risk, "lat=49.0&lon=31.0")[0] for _ in range(25)]  # noqa: SLF001
        assert 429 in codes

    def test_admin_tracks_requires_token(self):
        h = health_server._admin_tracks  # noqa: SLF001
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADMIN_DASH_TOKEN", None)
            assert self.get(h, headers={})[0] == 404
        with mock.patch.dict(os.environ, {"ADMIN_DASH_TOKEN": "x" * 24}):
            assert self.get(h, headers={})[0] == 401
            assert self.get(h, headers={"X-Admin-Token": "wrong"})[0] == 401
            status, body = self.get(h, headers={"X-Admin-Token": "x" * 24})
            assert status == 200 and body["tracks"] and body["tracks"][0]["speed_kmh"] is not None


    def test_sse_rev_uses_public_payload_only(self):
        a = health_server._compute_rev()  # noqa: SLF001
        priv = _private()
        shifted = json.loads(json.dumps(priv))
        for t in shifted["tracks"]:
            t["now"]["lat"] += 0.003
            t["speed_kmh"] += 50
            t["heading"] = 200
        seq = iter([priv, shifted])
        with mock.patch.object(health_server, "radar_payload", lambda db, m, now: next(seq)):
            r1, r2 = health_server._compute_rev(), health_server._compute_rev()  # noqa: SLF001
        assert r1 == r2 and a != ""


class StaticScanTests(unittest.TestCase):
    def test_clients_do_not_reference_admin_token_or_endpoint(self):
        from pathlib import Path
        root = Path(__file__).parent
        for rel in ("miniapp/radar.html", "miniapp/map.html", "miniapp/sw.js", "miniapp/native.js"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "X-Admin-Token" not in text and "/admin/" not in text, rel


class OldClientCompatTests(unittest.TestCase):
    """Старий клієнт (код, що зібраний у APK: app/www/radar.html) не падає на публічному форматі."""

    def test_old_apk_client_functions_accept_public_tracks(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("node не встановлено")
        # app/www — генерований (не в git) клиент APK; в CI проверяем те же функции из miniapp/radar.html.
        apk_copy = Path(__file__).parent / "app/www/radar.html"
        src = (apk_copy if apk_copy.exists() else Path(__file__).parent / "miniapp/radar.html").read_text(encoding="utf-8")
        m_pos = re.search(r"function positionAt\(t, ts\) \{.*?\n\}\n", src, re.S)
        m_noise = re.search(r"function isNoise\(t, ts\) \{.*?\n\}\n", src, re.S)
        assert m_pos and m_noise
        p = pv.build_public(_private(), NOW)
        script = ("const KEEP_AFTER_S = 25 * 60, EXTRAPOLATE_MAX_S = 20 * 60; const dest = () => [0, 0];\n" + m_pos.group(0) + m_noise.group(0) +
                  "const data = " + json.dumps(p) + ";\nlet shown = 0;\n"
                  "for (const t of data.tracks) { const pos = positionAt(t, data.now + 30); if (pos && !isNoise(t, data.now + 30)) shown++; }\n"
                  "console.log(JSON.stringify({n: data.tracks.length, shown}));")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)   # запуск node зазвичай < 0.3 с; запас на холодний старт під навантаженням
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout.strip())
        assert res["n"] > 0 and res["shown"] == res["n"]


class PublicImpactsTests(unittest.TestCase):
    OLD = NOW - 5 * 3600

    def _item(self, ts, slug="kyivska", oblast="Київська", place="Місто-А", killed=0, injured=0, kind="hit", lat=50.4501, lon=30.5234):
        return {"ts": ts, "when": "x", "oblast": oblast, "slug": slug, "place": place, "killed": killed, "injured": injured,
                "children": 0, "flags": "", "damage": ["energy"], "lat": lat, "lon": lon, "kind": kind, "precision": "place"}

    def _payload(self, items):
        return {"now": NOW, "delay_min": 0, "note": "n", "totals": {"killed": 99, "injured": 99, "hits": 99}, "items": items}

    def test_no_place_or_coordinates_and_delay_applied(self):
        out = pv.public_impacts(self._payload([self._item(self.OLD + 123, killed=1, injured=2),
                                               self._item(NOW - 600, slug="odeska", oblast="Одеська", place="Місто-Б")]))
        assert len(out["items"]) == 1                     # свіжий (менше 180 хв) прихований
        it = out["items"][0]
        assert it["place"] == "" and it["lat"] is None and it["lon"] is None and it["precision"] == "oblast"
        assert it["ts"] % pv.IMPACT_WINDOW_S == 0 and out["now"] % 300 == 0
        assert _floats_with_3_decimals(out) == []
        assert "Місто-А" not in json.dumps(out, ensure_ascii=False) and "Місто-Б" not in json.dumps(out, ensure_ascii=False)

    def test_events_in_same_oblast_and_window_are_aggregated_into_ranges(self):
        base = self.OLD // pv.IMPACT_WINDOW_S * pv.IMPACT_WINDOW_S
        out = pv.public_impacts(self._payload([self._item(base + 10, killed=1, injured=3), self._item(base + 900, killed=2, injured=1),
                                               self._item(base + 1800, killed=0, injured=0)]))
        assert len(out["items"]) == 1
        it = out["items"][0]
        assert it["hits_label"] == "2-3" and it["killed_range"] == "3-5" and it["injured_range"] == "3-5"
        assert not ({"killed", "injured", "children"} & set(it))          # точних чисел окремих записей немає

    def test_single_event_window_hides_small_counts(self):
        base = self.OLD // pv.IMPACT_WINDOW_S * pv.IMPACT_WINDOW_S
        it = pv.public_impacts(self._payload([self._item(base + 5, killed=2, injured=0)]))["items"][0]
        assert it["hits_label"] == "1" and it["killed_range"] == "1+" and it["injured_range"] == "0"

    def test_totals_are_rounded_so_diffs_do_not_leak_exact_counts(self):
        base = self.OLD // pv.IMPACT_WINDOW_S * pv.IMPACT_WINDOW_S
        t1 = pv.public_impacts(self._payload([self._item(base + 5, killed=3)]))["totals"]
        t2 = pv.public_impacts(self._payload([self._item(base + 5, killed=3), self._item(base - 4 * pv.IMPACT_WINDOW_S, killed=4)]))["totals"]
        assert t1["killed"] % 10 == 0 and t2["killed"] % 10 == 0 and t1["hits"] == 1 and t2["hits"] == 2
        assert t1["killed"] == 0 and t2["killed"] == 0                    # 3 и 7 не видны

    def test_kind_is_kept_per_group_only(self):
        base = self.OLD // pv.IMPACT_WINDOW_S * pv.IMPACT_WINDOW_S
        out = pv.public_impacts(self._payload([self._item(base + 5), self._item(base + 6, kind="heard")]))
        assert sorted(i["kind"] for i in out["items"]) == ["heard", "hit"] and all(i["hits_label"] == "1" for i in out["items"])

    def test_radar_client_skips_impacts_without_coordinates(self):
        from pathlib import Path
        src = (Path(__file__).parent / "miniapp/radar.html").read_text(encoding="utf-8")
        assert "filter(i => i.lat != null && i.lon != null)" in src

    def test_app_html_renders_aggregated_cards(self):
        from pathlib import Path
        src = (Path(__file__).parent / "miniapp/app.html").read_text(encoding="utf-8")
        assert "aggCard" in src and "killed_range" in src and "hits_label" in src


class MapPinsAreRegionLevelTests(unittest.TestCase):
    def test_map_pins_ignore_city_names(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("node не встановлено")
        src = (Path(__file__).parent / "miniapp/map.html").read_text(encoding="utf-8")
        m1 = re.search(r"function pinSlugOf\(t\) \{.*?\n\}\n", src, re.S)
        m2 = re.search(r"function pinsOf\(t\) \{.*?\n\}\n", src, re.S)
        assert m1 and m2
        script = ("const REGION_COORDS = {kyivska: [50.0, 30.5]}, REGION_NAMES = {kyivska: 'Київська'}, CITY_COORDS = {x: {c: [50.4501, 30.5234], name: 'Місто', region: 'kyivska'}};\n"
                  "const citySlugsOf = () => ['x'];\n" + m1.group(0) + m2.group(0) +
                  "console.log(JSON.stringify(pinsOf({region: 'kyivska', text: 'Шахеди на Місто, лівий берег'})));")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)   # запуск node зазвичай < 0.3 с; запас на холодний старт під навантаженням
        assert out.returncode == 0, out.stderr
        pins = json.loads(out.stdout.strip())
        assert len(pins) == 1 and pins[0]["isCity"] is False and pins[0]["coord"] == [50.0, 30.5] and pins[0]["key"] == "kyivska"


class OtherPublicPayloadsTests(unittest.TestCase):
    """stats.json и alerts_hist.json (/api/stats, /api/alerts-history): без координат и служебных ключей позиции."""

    def test_stats_and_history_have_no_coordinates(self):
        from stats_engine import stats_payload
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            now = int(time.time())
            for payload in (stats_payload(db, now), health_server.history_payload(db, now, 7)):
                assert not ({k for k, _ in _walk(payload)} & {"lat", "lon", "points", "forecast", "dest", "heading", "speed_kmh"})
                assert _floats_with_3_decimals(payload) == []
        finally:
            db.close()
            os.unlink(file.name)


class ImpactsWiringTests(unittest.TestCase):
    def test_public_tracks_payload_impacts_have_no_place_or_coordinates(self):
        priv = _private()
        priv["impacts"] = [{"ts": NOW - 5 * 3600, "oblast": "Київська", "slug": "kyivska", "place": "Місто-А", "lat": 50.4501,
                            "lon": 30.5234, "kind": "hit", "killed": 1, "injured": 0, "children": 0, "damage": [], "flags": ""},
                           {"ts": NOW - 300, "oblast": "Одеська", "slug": "odeska", "place": "Місто-Б", "lat": 46.4825,
                            "lon": 30.7233, "kind": "hit", "killed": 0, "injured": 0, "children": 0, "damage": [], "flags": ""}]
        out = pv.build_public(priv, NOW)
        assert len(out["impacts"]) == 1                      # свіжий прилёт затримано
        text = json.dumps(out, ensure_ascii=False)
        assert "Місто-А" not in text and "Місто-Б" not in text
        assert all(i["lat"] is None and i["lon"] is None and i["place"] == "" for i in out["impacts"])
        assert _floats_with_3_decimals(out) == []

    def test_export_writes_public_impacts_file(self):
        from pathlib import Path
        src = (Path(__file__).parent / "export_radar.py").read_text(encoding="utf-8")
        assert "public_impacts(impacts_payload(" in src

    def test_client_cache_key_rotated_and_old_one_cleared(self):
        from pathlib import Path
        src = (Path(__file__).parent / "miniapp/radar.html").read_text(encoding="utf-8")
        assert "ar_last_tracks_v2" in src and "removeItem('ar_last_tracks')" in src


class PublicTextTests(unittest.TestCase):
    RAW = "🛸 Рух шахедів станом на 13:45: 4 на Сумщині — Суми, Низи; курсом на Куп'янськ. вул. Хрещатик 12 — удар по ТЕЦ"

    def test_generic_text_has_no_places_or_directions(self):
        t = pv.public_threat_text("shahed", "sumska")
        assert t.startswith("БпЛА — ") and not any(w in t for w in ("Суми", "Низи", "Куп", "вул", "ТЕЦ", "курс"))

    def test_strip_threat_replaces_text_rounds_time_and_sources(self):
        item = {"ts": NOW + 137, "type": "shahed", "region": "sumska", "text": self.RAW, "age_min": 7, "sources": 9,
                "status": "corroborated", "track": {"speed_kmh": 150}, "origin": "x", "destination": "y"}
        out = pv.strip_threat(item)
        assert "Хрещатик" not in json.dumps(out, ensure_ascii=False) and "Куп" not in out["text"]
        assert out["ts"] % 300 == 0 and out["age_min"] == 5 and out["sources"] == 3 and "track" not in out

    def test_unknown_type_is_generic(self):
        assert pv.public_threat_text("???", "") == "Загроза"

    def test_feed_in_public_payload_is_generic_and_deduplicated(self):
        priv = _private()
        priv["feed"] = [{"ts": NOW - 200, "text": self.RAW, "region": "sumska", "type": "shahed"},
                        {"ts": NOW - 190, "text": "інший текст зі Сумами", "region": "sumska", "type": "shahed"}]
        out = pv.build_public(priv, NOW)
        assert len(out["feed"]) == 1 and "Суми" not in json.dumps(out["feed"], ensure_ascii=False)


class ReportsCoarsenTests(unittest.TestCase):
    def test_user_report_is_coarsened_and_text_hidden(self):
        r = {"ts": NOW + 77, "lat": 48.6012, "lon": 35.3011, "region": "dnipropetrovska", "text": "бачив шахед над вул. Садовою 5", "age_min": 7}
        out = pv.coarsen_report(r)
        assert out["text"] == "Повідомлення користувача" and "Садов" not in json.dumps(out, ensure_ascii=False)
        assert out["ts"] % 300 == 0 and out["age_min"] == 5
        assert _floats_with_3_decimals(out) == [] and (out["lat"], out["lon"]) != (r["lat"], r["lon"])

    def test_report_outside_raions_is_dropped(self):
        assert pv.coarsen_report({"ts": NOW, "lat": 10.0, "lon": 10.0, "text": "x"}) is None
