"""Надійність доставки тривог: постійні помилки, скасування, ізоляція отримувачів, відновлення, дедуп, FloodWait."""
import asyncio

import pytest

from airradar.delivery import notify_queue as nq
from airradar.delivery import tg_delivery
from airradar.ops import migrations
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "d.db"))
    migrations.apply_pending(d)
    yield d
    d.close()


class UserIsBlockedError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class FakeBot:
    """Записує відправки; для вказаних user_id кидає задані помилки."""

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail or {}

    async def send_message(self, user_id, text, **kw):
        if user_id in self.fail:
            raise self.fail[user_id]
        self.sent.append((user_id, text))

        class M:
            id = len(self.sent)
        return M()


def _status(db, user_id):
    return db._conn.execute("SELECT status, attempts, last_error FROM notification_queue WHERE user_id=?", (user_id,)).fetchone()


def test_permanent_error_fails_immediately_without_retries(db):
    bot = FakeBot({1: UserIsBlockedError()})
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "🚨 ПОВІТРЯНА ТРИВОГА")) is False
    row = _status(db, 1)
    assert row["status"] == "failed" and row["attempts"] == 1 and row["last_error"] == "UserIsBlockedError"
    assert nq.claim_due(db, now=10**10) == []          # воркер не повторює


def test_temporary_error_goes_to_retry_with_backoff(db):
    bot = FakeBot({1: ConnectionError("net")})
    asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "текст"))
    assert _status(db, 1)["status"] == "retrying"


def test_floodwait_respects_retry_after(db):
    bot = FakeBot({1: FloodWaitError(90)})
    asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "текст"))
    row = db._conn.execute("SELECT status, next_attempt_ts, created_ts FROM notification_queue").fetchone()
    assert row["status"] == "retrying" and row["next_attempt_ts"] - row["created_ts"] >= 89


def test_one_failing_recipient_does_not_block_others(db):
    bot = FakeBot({2: UserIsBlockedError()})
    results = [asyncio.run(tg_delivery.deliver(bot, db, "k", u, "🚨 ТРИВОГА")) for u in (1, 2, 3)]
    assert results == [True, False, True] and [u for u, _ in bot.sent] == [1, 3]


def test_same_alert_to_same_user_not_sent_twice(db):
    bot = FakeBot()
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "🚨 ТРИВОГА")) is True
    assert asyncio.run(tg_delivery.deliver(bot, db, "k", 1, "🚨 ТРИВОГА")) is False
    assert len(bot.sent) == 1


def test_cancelled_send_is_not_left_in_sending(db):
    class Hang(FakeBot):
        async def send_message(self, *a, **k):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tg_delivery.deliver(Hang(), db, "k", 1, "текст"))
    assert _status(db, 1)["status"] == "retrying"


def test_stuck_sending_recovered_after_restart_and_resent(db):
    import time
    nq.enqueue(db, "ev", 5, "tg", payload='{"text": "🚨 ТРИВОГА"}')
    nq.claim_due(db)                                    # «процес впав» після claim: запис у sending
    assert _status(db, 5)["status"] == "sending"
    assert nq.claim_due(db, now=int(time.time()) + 5) == []          # у sending воркер його не бере
    assert nq.recover_after_restart(db) == 1
    assert _status(db, 5)["status"] == "retrying"
    jobs = nq.claim_due(db, now=int(time.time()) + 5)
    assert [j["user_id"] for j in jobs] == [5]          # після рестарту запис знову доступний воркеру


def test_is_permanent_classification():
    assert nq.is_permanent(UserIsBlockedError()) and not nq.is_permanent(ConnectionError()) and not nq.is_permanent(FloodWaitError(5))
