"""SQLite: режим WAL, busy_timeout, конкурентні записи з потоків, відкат після помилки, повторна ініціалізація."""
import sqlite3
import threading

import pytest

from airradar.ops import migrations
from database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "c.db"))
    migrations.apply_pending(d)
    yield d
    d.close()


def test_wal_and_busy_timeout_configured(db):
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_concurrent_writers_lose_nothing(db):
    errors = []

    def work(t):
        try:
            for i in range(30):
                db.record_delivery(f"k{t}-{i}", f"tg:{t}", "info", "sent")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert db._conn.execute("SELECT COUNT(*) FROM notification_delivery").fetchone()[0] == 8 * 30


def test_readers_not_blocked_by_writer_in_other_connection(tmp_path):
    """WAL: читач окремого з'єднання не блокується відкритою транзакцією запису."""
    path = str(tmp_path / "w.db")
    d = Database(path)
    migrations.apply_pending(d)
    d.record_delivery("seed", "tg:1", "info", "sent")
    w = sqlite3.connect(path, timeout=1)
    w.execute("BEGIN IMMEDIATE")
    w.execute("INSERT INTO notification_delivery (event_key, channel, importance, status, created_ts, updated_ts) VALUES ('x','tg:2','info','sent',0,0)")
    r = sqlite3.connect(path, timeout=1)
    assert r.execute("SELECT COUNT(*) FROM notification_delivery").fetchone()[0] == 1   # бачить лише зафіксоване
    w.rollback()
    w.close(); r.close(); d.close()


def test_rollback_leaves_no_partial_data(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db._lock:
            db._conn.execute("INSERT INTO notification_queue (event_key,user_id,channel,status,next_attempt_ts,created_ts,expires_ts) VALUES ('a',1,'tg','pending',0,0,9)")
            db._conn.execute("INSERT INTO notification_queue (event_key,user_id,channel,status,next_attempt_ts,created_ts,expires_ts) VALUES ('a',1,'tg','pending',0,0,9)")
    db._conn.rollback()
    assert db._conn.execute("SELECT COUNT(*) FROM notification_queue").fetchone()[0] == 0


def test_reopen_existing_db_keeps_data_and_is_idempotent(tmp_path):
    path = str(tmp_path / "r.db")
    d = Database(path); migrations.apply_pending(d)
    d.record_delivery("k", "tg:1", "info", "sent"); d.close()
    d2 = Database(path); migrations.apply_pending(d2); migrations.apply_pending(d2)
    assert d2._conn.execute("SELECT COUNT(*) FROM notification_delivery").fetchone()[0] == 1
    d2.close()
