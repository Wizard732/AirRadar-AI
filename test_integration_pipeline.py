"""Інтеграційні сценарії: парсер → політика → trace → черга сповіщень → скасування/відновлення; відмови компонентів."""
import sqlite3

import pytest

import safety_policy
import threat_parser
from airradar.delivery import notify_queue
from airradar.monitoring import incident_trace
from airradar.ops import migrations
from airradar.security import admin_access
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "i.db"))
    migrations.apply_pending(d)
    yield d
    d.close()


def _pipeline(db, key, text, users=(1, 2)):
    incident_trace.record(db, key, "received", source="@src", data={"text": text})
    parsed = threat_parser.parse_post(text)
    incident_trace.record(db, key, "parse", source="@src", data={"obs": len(parsed.observations)})
    verdict = safety_policy.check_publication(text, llm_generated=True)
    incident_trace.record(db, key, "safety", ok=verdict.allowed, data={"violations": verdict.violations})
    if not verdict.allowed:
        incident_trace.record(db, key, "rejected", ok=False)
        return 0
    incident_trace.record(db, key, "published")
    return sum(notify_queue.enqueue(db, key, u, "tg") for u in users)


def test_happy_path_end_to_end(db):
    n = _pipeline(db, "ev1", "Група БпЛА рухається на Київ")
    assert n == 2
    jobs = notify_queue.claim_due(db)
    for j in jobs:
        notify_queue.mark_sent(db, j["id"], "m")
    stages = [s["stage"] for s in incident_trace.timeline(db, "ev1")]
    assert stages == ["received", "parse", "safety", "published"]
    assert notify_queue.stats(db, 0)["sent"] == 2


def test_duplicate_event_not_delivered_twice(db):
    assert _pipeline(db, "ev1", "БпЛА на Київ") == 2
    assert _pipeline(db, "ev1", "БпЛА на Київ") == 0


def test_unsafe_text_rejected_and_never_queued(db):
    assert _pipeline(db, "ev2", "Ракета влучить в завод, точка 50.45123, 30.52345") == 0
    assert db._conn.execute("SELECT COUNT(*) FROM notification_queue").fetchone()[0] == 0
    assert incident_trace.timeline(db, "ev2")[-1]["stage"] == "rejected"


def test_malformed_message_does_not_crash(db):
    for text in ("", "   ", "\x00\x00", "a" * 50000, "🚀" * 100):
        incident_trace.record(db, "bad", "received", data={"text": text})
        threat_parser.parse_post(text)


def test_event_ended_before_delivery_cancels(db):
    _pipeline(db, "ev3", "БпЛА на Київ")
    assert notify_queue.cancel_for_event(db, "ev3") == 2
    assert notify_queue.claim_due(db) == []


def test_push_failure_retries_then_fails_without_loss(db):
    notify_queue.enqueue(db, "ev4", 1, "webpush", ttl_s=10**6, now=0)
    t, status = 0, None
    for _ in range(notify_queue.MAX_ATTEMPTS):
        job = notify_queue.claim_due(db, now=t)[0]
        status = notify_queue.mark_failed(db, job["id"], "ConnectionError", now=t)
        t += notify_queue.MAX_BACKOFF_S
    assert status == "failed"
    row = db._conn.execute("SELECT status, attempts, last_error FROM notification_queue").fetchone()
    assert row["attempts"] == notify_queue.MAX_ATTEMPTS and row["last_error"] == "ConnectionError"


def test_process_restart_recovers_queue(tmp_path):
    p = str(tmp_path / "r.db")
    d = Database(p); notify_queue.enqueue(d, "ev5", 1, "tg"); notify_queue.claim_due(d); d.close()   # «падіння» під час sending
    d2 = Database(p)
    assert notify_queue.recover_after_restart(d2) == 1
    assert notify_queue.claim_due(d2)
    d2.close()


def test_database_unavailable_does_not_break_audit_or_trace(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    d.close()
    # після закриття з'єднання аудит/trace мовчки деградують, а не кидають виняток у конвеєр
    admin_access.log_action(d, 1, "x")
    incident_trace.record(d, "k", "received")


def test_bad_migration_leaves_data_and_version(tmp_path, monkeypatch):
    d = Database(str(tmp_path / "m.db")); d.add_threat("uav", "Київська", "keep", "@s")
    def boom(_db):
        raise sqlite3.OperationalError("broken")
    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "baseline", lambda x: None), (2, "bad", boom)])
    with pytest.raises(sqlite3.OperationalError):
        migrations.apply_pending(d)
    assert migrations.current_version(d) == 1                      # зафіксована лише успішна міграція
    assert d._conn.execute("SELECT COUNT(*) FROM threats").fetchone()[0] == 1
    d.close()


def test_conflicting_sources_do_not_confirm():
    import trust
    p = trust.build_provenance(1000, 1010, ["@a", "@b"], overrides={"a": "net", "b": "net"})
    assert trust.classify(p, 1100) is trust.Confidence.PRELIMINARY


def test_stale_event_classified_stale():
    import trust
    p = trust.build_provenance(1000, 1010, ["@a", "@b"])
    assert trust.classify(p, 1000 + trust.DEFAULT_TTL_S + 1) is trust.Confidence.STALE


# ---------- доставка Telegram через чергу ----------
import asyncio  # noqa: E402

from airradar.delivery import tg_delivery  # noqa: E402


class _Bot:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    async def send_message(self, uid, text, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((uid, text))
        return type("M", (), {"id": 7})()


class _Flood(Exception):
    seconds = 30


def test_deliver_sends_once_and_dedups(db):
    bot = _Bot()
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "текст")) is True
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "текст")) is False       # той самий пост
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "оновлений текст")) is True
    assert len(bot.sent) == 2 and notify_queue.stats(db, 0)["sent"] == 2


def test_deliver_failure_goes_to_retry_and_worker_resends(db):
    bad = _Bot(fail=_Flood())
    assert asyncio.run(tg_delivery.deliver(bad, db, "k", 1, "тривога")) is False
    row = db._conn.execute("SELECT status, next_attempt_ts, last_error FROM notification_queue").fetchone()
    assert row["status"] == "retrying" and row["last_error"] == "_Flood"
    import time
    assert row["next_attempt_ts"] - time.time() >= 25                                # FloodWait поважається
    db._conn.execute("UPDATE notification_queue SET next_attempt_ts = 0")
    good = _Bot()

    async def run():
        t = asyncio.create_task(tg_delivery.retry_worker(good, db, interval_s=3600))
        await asyncio.sleep(0.2); t.cancel()
    asyncio.run(run())
    assert good.sent == [(1, "тривога")] and notify_queue.stats(db, 0)["sent"] == 1


def test_same_text_next_window_is_delivered_again():
    a = tg_delivery.event_key_for("k", "t", now=0)
    assert a == tg_delivery.event_key_for("k", "t", now=100)
    assert a != tg_delivery.event_key_for("k", "t", now=tg_delivery.DEDUP_BUCKET_S + 1)


# ---------- push та канал через чергу ----------
from airradar.delivery import push_queue  # noqa: E402


def test_push_queue_deliver_dedup_and_retry(db):
    calls = {"n": 0, "ok": False}

    async def sender(_db, uid, payload):
        calls["n"] += 1
        return 1 if calls["ok"] else 0
    push_queue.register("t", sender)
    p = {"title": "T", "body": "B"}
    assert asyncio.run(push_queue.deliver(db, "t", "k", 1, p)) == 0                  # пристрій не прийняв → retrying
    assert notify_queue.stats(db, 0)["retrying"] == 1
    db._conn.execute("UPDATE notification_queue SET next_attempt_ts = 0")
    calls["ok"] = True

    async def run():
        t = asyncio.create_task(push_queue.retry_worker(db, interval_s=3600))
        await asyncio.sleep(0.2); t.cancel()
    asyncio.run(run())
    assert notify_queue.stats(db, 0)["sent"] == 1
    assert asyncio.run(push_queue.deliver(db, "t", "k", 1, p)) == 0                  # той самий пуш у вікні — дубль
    assert calls["n"] == 2


def test_push_sender_exception_is_contained(db):
    async def boom(_db, uid, payload):
        raise RuntimeError("x")
    push_queue.register("b", boom)
    assert asyncio.run(push_queue.deliver(db, "b", "", 1, {"title": "a", "body": "b"})) == 0
    assert notify_queue.stats(db, 0)["retrying"] == 1


def test_publisher_retries_transient_and_queues_outbox(db):
    import publisher as pub

    class Resp:
        def __init__(self, status, data): self.status, self._d = status, data
        async def json(self): return self._d
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Sess:
        closed = False
        def __init__(self, seq): self.seq, self.n = seq, 0
        def post(self, url, json=None):
            r = self.seq[min(self.n, len(self.seq) - 1)]; self.n += 1; return r

    p = pub.Publisher("1:x", "@c", 5, session=Sess([Resp(500, {"ok": False}), Resp(200, {"ok": True, "result": {"message_id": 5}})]))
    orig = asyncio.sleep
    async def fast(_): return None
    asyncio.sleep = fast
    try:
        assert asyncio.run(p._deliver("sendMessage", {"text": "x"}))["message_id"] == 5     # 500 → повтор → успіх
        bad = pub.Publisher("1:x", "@c", 5, session=Sess([Resp(400, {"ok": False, "description": "bad"})]))
        assert asyncio.run(bad._deliver("sendMessage", {"text": "x"})) is False and bad._last_transient is False   # 400 не повторюється
        down = pub.Publisher("1:x", "@c", 5, session=Sess([Resp(503, {"ok": False})]))
        down.outbox_db = db
        assert asyncio.run(down.send("🚨 тест повідомлення")) is False
    finally:
        asyncio.sleep = orig
    assert notify_queue.stats(db, 0)["pending"] == 1                                        # поставлено в outbox
    row = db._conn.execute("SELECT channel FROM notification_queue").fetchone()
    assert row["channel"] == "channel"


def test_main_module_prefers_running_main(monkeypatch):
    import sys
    import types

    from airradar.app_ref import main_module
    fake = types.ModuleType("__main__"); fake._notify_subscribers = lambda *a: None
    monkeypatch.setitem(sys.modules, "__main__", fake)
    assert main_module() is fake                       # не створює другу копію main
