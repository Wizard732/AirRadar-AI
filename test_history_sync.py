from __future__ import annotations

import os
import tempfile
import unittest

from database import Database
from sync_history import event_fields


class HistorySyncDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()
        os.unlink(self.path)

    def test_message_claim_and_cursor_are_idempotent(self) -> None:
        self.assertTrue(self.db.claim_history_message("channel", 42, 1_700_000_000))
        self.assertFalse(self.db.claim_history_message("channel", 42, 1_700_000_000))
        self.db.update_history_cursor("channel", 42, 1_700_000_000)
        self.db.update_history_cursor("channel", 41, 1_699_999_000)
        self.assertEqual(self.db.get_history_cursor("channel"), (42, 1_700_000_000))
        self.assertEqual(self.db.get_history_reconcile_min_id("channel", 1_699_000_000), 42)

    def test_historical_timestamp_is_preserved(self) -> None:
        self.db.add_threat("missile", "kyivska", "test", "channel", event_ts=1_700_000_000)
        with self.db._lock:
            assert self.db._conn is not None
            row = self.db._conn.execute("SELECT ts FROM threats").fetchone()
        self.assertEqual(row["ts"], 1_700_000_000)

    def test_historical_alert_interval_uses_event_times(self) -> None:
        self.db.alert_start("kyivska", event_ts=1_700_000_000)
        self.db.alert_end("kyivska", event_ts=1_700_000_600)
        with self.db._lock:
            assert self.db._conn is not None
            row = self.db._conn.execute("SELECT started_ts, ended_ts FROM alerts").fetchone()
        self.assertEqual((row["started_ts"], row["ended_ts"]), (1_700_000_000, 1_700_000_600))

    def test_event_fields_marks_all_clear(self) -> None:
        _, weapon, stage = event_fields("Відбій повітряної тривоги")
        self.assertEqual(weapon, "stand_down")
        self.assertEqual(stage, "all_clear")


if __name__ == "__main__":
    unittest.main()
