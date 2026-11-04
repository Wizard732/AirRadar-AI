"""Mixin Database: охрана и предупреждения по районам."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


class GuardMixin:
    """охрана и предупреждения по районам. Использует self._lock и self._conn из Database."""


    # --- Гео-сторож -----------------------------------------------------
    def set_guard(self, user_id: int, enabled: bool, radius_km: float | None = None) -> None:
        """Включить/выключить сторож. При выключении точка и состояние стираются."""
        try:
            with self._lock:
                assert self._conn is not None
                if enabled:
                    self._conn.execute(
                        "INSERT INTO guard_points (user_id, enabled, radius_km) VALUES (?, 1, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET enabled = 1, radius_km = COALESCE(?, radius_km)",
                        (user_id, radius_km or 10.0, radius_km),
                    )
                else:
                    self._conn.execute("DELETE FROM guard_points WHERE user_id = ?", (user_id,))
                    self._conn.execute("DELETE FROM guard_state WHERE user_id = ?", (user_id,))
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить гео-сторож: %s", exc)


    def get_guard(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT * FROM guard_points WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


    def update_guard_location(self, user_id: int, lat: float, lon: float, live: bool, now: int | None = None) -> bool:
        """Обновить позицию; True, если сторож у пользователя включён."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "UPDATE guard_points SET lat = ?, lon = ?, live = ?, updated_ts = ? "
                    "WHERE user_id = ? AND enabled = 1",
                    (lat, lon, 1 if live else 0, now if now is not None else int(time.time()), user_id),
                )
                self._conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.warning("Не удалось обновить позицию сторожа: %s", exc)
            return False


    def active_guards(self, now: int, live_ttl: int = 600) -> list[dict[str, Any]]:
        """Сторожа с позицией: live-точка живёт live_ttl секунд без обновлений, фиксированная — всегда."""
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM guard_points WHERE enabled = 1 AND lat IS NOT NULL AND "
                "(live = 0 OR updated_ts >= ?)", (now - live_ttl,),
            ).fetchall()
        return [dict(r) for r in rows]


    def guard_states(self, user_id: int) -> dict[str, dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT * FROM guard_state WHERE user_id = ?", (user_id,)).fetchall()
        return {r["track_key"]: dict(r) for r in rows}


    def set_guard_state(self, user_id: int, track_key: str, level: str, distance: float, now: int) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO guard_state (user_id, track_key, level, distance, sent_ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, track_key) DO UPDATE SET level = ?, distance = ?, sent_ts = ?",
                (user_id, track_key, level, distance, now, level, distance, now),
            )
            self._conn.commit()


    def clear_guard_state(self, user_id: int, track_key: str) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM guard_state WHERE user_id = ? AND track_key = ?", (user_id, track_key))
            self._conn.commit()


    def district_warned(self, track_key: str, region: str) -> int | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT sent_ts FROM district_warn_state WHERE track_key = ? AND region = ?", (track_key, region)
            ).fetchone()
        return int(row["sent_ts"]) if row else None


    def district_last_for_region(self, region: str) -> int:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT MAX(sent_ts) AS ts FROM district_warn_state WHERE region = ?", (region,)
            ).fetchone()
        return int(row["ts"] or 0)


    def mark_district_warned(self, track_key: str, region: str, now: int) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "INSERT OR REPLACE INTO district_warn_state (track_key, region, sent_ts) VALUES (?, ?, ?)",
                (track_key, region, now),
            )
            self._conn.execute("DELETE FROM district_warn_state WHERE sent_ts < ?", (now - 86400,))
            self._conn.commit()
