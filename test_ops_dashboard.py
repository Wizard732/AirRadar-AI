"""Тести дашборда, адмін-ендпоінтів і публічної затримки живого API."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

import health_server
from airradar.monitoring import dashboard, metrics
from airradar.ops import retention
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()


def test_collect_reports_unknown_not_zero(db, monkeypatch):
    monkeypatch.setattr("airradar.ops.backup.backup_dir", lambda: __import__("pathlib").Path("/nonexistent-q"))
    d = dashboard.collect(db, "24h")
    assert d["system"]["last_backup_age_s"] is None
    assert d["delivery"]["delivery_delay_p50_s"] is None
    assert d["processing"]["pipeline_legacy"]["p95_ms"] is None
    assert d["processing"]["latency"]["parse"]["p95"] is None
    assert "unknown" in dashboard.render_text(d)


def test_collect_periods_and_json_serializable(db):
    metrics.record_stage(db, "total", 120)
    for p in ("1h", "24h", "7d", "30d"):
        d = dashboard.collect(db, p)
        json.dumps(d)
        assert d["processing"]["latency"]["total"]["count"] == 1
    dashboard.collect(db, "bogus")  # невідомий період → вікно 24h, не падає


def test_token_requires_configuration(monkeypatch):
    monkeypatch.delenv("ADMIN_DASH_TOKEN", raising=False)
    assert not dashboard.token_ok("")
    monkeypatch.setenv("ADMIN_DASH_TOKEN", "short")
    assert not dashboard.token_ok("short")          # занадто короткий токен не приймається
    monkeypatch.setenv("ADMIN_DASH_TOKEN", "x" * 24)
    assert dashboard.token_ok("x" * 24) and not dashboard.token_ok("y" * 24)


def _client(db):
    health_server.set_db(db)
    app = web.Application()
    app.router.add_get("/admin/health", health_server._admin_health)
    app.router.add_get("/admin/trace", health_server._admin_trace)
    app.router.add_get("/api/tracks", health_server._api_tracks)
    return TestClient(TestServer(app))


def test_admin_endpoints_auth(db, monkeypatch):
    async def run():
        async with _client(db) as c:
            monkeypatch.delenv("ADMIN_DASH_TOKEN", raising=False)
            assert (await c.get("/admin/health")).status == 404                       # вимкнено
            monkeypatch.setenv("ADMIN_DASH_TOKEN", "t" * 24)
            assert (await c.get("/admin/health")).status == 401
            assert (await c.get("/admin/health?token=" + "t" * 24)).status == 401     # токен у URL не діє
            r = await c.get("/admin/health", headers={"X-Admin-Token": "t" * 24})
            assert r.status == 200 and "system" in await r.json()
            assert (await c.get("/admin/trace")).status == 401
    asyncio.run(run())


def test_live_api_applies_public_delay(db, monkeypatch):
    seen = {}
    monkeypatch.setattr(health_server, "radar_payload", lambda d, m, now: seen.setdefault("now", now) and {"tracks": []})
    monkeypatch.setenv("TRACKS_PUBLIC_DELAY_MIN", "10")
    import time

    async def run():
        async with _client(db) as c:
            t0 = int(time.time())
            assert (await c.get("/api/tracks")).status == 200
            assert 590 <= t0 - seen["now"] <= 610
    asyncio.run(run())


def test_retention_prunes_only_service_data(db):
    from airradar.monitoring import incident_trace
    incident_trace.record(db, "old", "received", now=1.0)
    incident_trace.record(db, "new", "received")
    db.add_threat("uav", "Київська", "keep", "@s")
    out = retention.run(db)
    assert out["event_trace"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM threats").fetchone()[0] == 1
    assert incident_trace.timeline(db, "new")


def test_sse_stream_emits_rev_and_limits(db, monkeypatch):
    monkeypatch.setattr(health_server, "radar_payload", lambda d, m, now: {"tracks": [], "now": now})
    health_server._stream_rev.update({"rev": "", "ts": 0.0})

    async def run():
        health_server.set_db(db)
        app = web.Application()
        app.router.add_get("/api/stream", health_server._api_stream)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/stream")
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
            chunk = await asyncio.wait_for(r.content.readline(), 5)
            assert chunk.startswith(b"event: rev")
            r.close()
            await asyncio.sleep(0.3)   # дати обробнику завершитись і звільнити слот
            health_server._sse_clients[0] = health_server._SSE_MAX
            assert (await c.get("/api/stream")).status == 503
            health_server._sse_clients[0] = 0
    asyncio.run(run())
