# -*- coding: utf-8 -*-
"""Атомарність «подія → notify_queue»: транзакції, ідемпотентність, lease, fault injection (без реального Telegram)."""
import asyncio
import threading
import time

import pytest

from airradar.delivery import notify_queue as nq, tg_delivery
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "q.db")); yield d; d.close()


def _count(db, where="1=1"):
    return db._conn.execute(f"SELECT COUNT(*) c FROM notification_queue WHERE {where}").fetchone()["c"]


def test_enqueue_many_all_or_nothing_on_midway_failure(db):
    rows = [("k", u, "p") for u in (1, 2, 3)]
    real = db._conn
    calls = {"n": 0}

    class Flaky:
        def __getattr__(self, name): return getattr(real, name)
        def execute(self, sql, *a):
            if sql.lstrip().startswith("INSERT OR IGNORE"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("збій на другій вставці")
            return real.execute(sql, *a)
        def rollback(self): return real.rollback()
        def commit(self): return real.commit()
    nq.ensure_schema(db)
    db._conn = Flaky()
    with pytest.raises(RuntimeError):
        nq.enqueue_many(db, rows, "tg")
    db._conn = real
    assert _count(db) == 0, "відкат: жодного запису не лишилось"
    assert nq.enqueue_many(db, rows, "tg") == 3 and _count(db) == 3


def test_enqueue_many_idempotent(db):
    rows = [("k", u, "p") for u in (1, 2)]
    assert nq.enqueue_many(db, rows, "tg") == 2
    assert nq.enqueue_many(db, rows, "tg") == 0 and _count(db) == 2


def test_concurrent_enqueue_same_event_no_duplicates(db):
    rows = [("evt", u, "p") for u in range(50)]
    errs = []
    def run():
        try: nq.enqueue_many(db, rows, "tg")
        except Exception as e: errs.append(e)
    ts = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and _count(db) == 50


def test_two_claims_never_return_same_job(db):
    nq.enqueue_many(db, [("k", u, "p") for u in range(10)], "tg")
    a, b = nq.claim_due(db, 6), nq.claim_due(db, 6)
    ids_a, ids_b = {r["id"] for r in a}, {r["id"] for r in b}
    assert len(a) == 6 and len(b) == 4 and not (ids_a & ids_b)


def test_sending_lease_reclaims_hung_job_but_not_fresh_one(db):
    nq.enqueue_many(db, [("k", 1, "p"), ("k", 2, "p")], "tg")
    now = int(time.time())
    jobs = nq.claim_due(db, 10, now=now)
    assert len(jobs) == 2 and nq.claim_due(db, 10, now=now + 10) == []        # в роботі, lease не минув
    db._conn.execute("UPDATE notification_queue SET claimed_ts=? WHERE user_id=1", (now - nq.SENDING_LEASE_S - 5,)); db._conn.commit()
    again = nq.claim_due(db, 10, now=now + 11)
    assert [r["user_id"] for r in again] == [1], "завислий запис повернувся в роботу, свіжий — ні"


def test_restart_recovers_sending(db):
    nq.enqueue_many(db, [("k", 1, "p")], "tg"); nq.claim_due(db, 5)
    assert nq.recover_after_restart(db) == 1
    assert [r["user_id"] for r in nq.claim_due(db, 5, now=int(time.time()) + 5)] == [1]


def test_sent_job_is_never_returned_to_pending(db):
    nq.enqueue_many(db, [("k", 1, "p")], "tg"); j = nq.claim_due(db, 5)[0]
    nq.mark_sent(db, j["id"], "42")
    assert nq.recover_after_restart(db) == 0
    assert nq.claim_due(db, 5, now=int(time.time()) + 10**6) == []
    assert nq.enqueue_many(db, [("k", 1, "p")], "tg") == 0 and _count(db, "status='sent'") == 1


def test_fault_after_enqueue_before_send_worker_delivers_after_restart(db, monkeypatch):
    """Крах після commit, до мережі: записи лишаються pending, воркер доставляє всіх; збій одного не блокує інших."""
    recips = [(u, "🛵 Київщина: БпЛА") for u in (1, 2, 3)]
    tg_delivery.enqueue_batch(db, "k", recips, feedback_key="k", src_text="s")
    assert _count(db, "status='pending'") == 3          # «процес упав» тут: нічого не відправлено
    sent = []

    async def fake_send(bot, user_id, payload):
        if user_id == 2:
            raise RuntimeError("timeout")
        sent.append(user_id); return "1"
    monkeypatch.setattr(tg_delivery, "_send", fake_send)

    async def one_pass():
        t = asyncio.create_task(tg_delivery.retry_worker(None, db, interval_s=0))
        await asyncio.sleep(0.2); t.cancel()
        try: await t
        except asyncio.CancelledError: pass
    asyncio.run(one_pass())
    assert sorted(sent) == [1, 3]
    st = {r["user_id"]: r["status"] for r in db._conn.execute("SELECT user_id,status FROM notification_queue")}
    assert st[1] == st[3] == "sent" and st[2] in ("retrying", "failed")


def test_deliver_uses_batch_key_across_bucket_boundary(db, monkeypatch):
    """Ключ події береться з постановки в чергу (не перераховується): межа 10-хв відра не дає другого запису."""
    monkeypatch.setattr(tg_delivery, "_send", lambda *a, **k: asyncio.sleep(0, result="1"))
    recips = [(1, "t")]
    keys = tg_delivery.enqueue_batch(db, "k", recips, now=599)            # кінець відра
    monkeypatch.setattr(tg_delivery, "event_key_for", lambda d, t, now=None: "ІНШИЙ")   # deliver перерахував би інакше
    assert asyncio.run(tg_delivery.deliver(None, db, "k", 1, "t", event_key=keys[(1, "t")])) is True
    assert _count(db) == 1 and _count(db, "status='sent'") == 1
