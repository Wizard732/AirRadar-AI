"""Домен «db»: адміністратори та здоров'я каналів (виділено з database.py без зміни API).

Mixin: методи доступні як `Database.add_admin(...)` та ін. — зворотна сумісність повна.
"""
from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


class AdminChannelsMixin:
    # ------------------------------------------------------------------
    #  Админка: управление администраторами
    # ------------------------------------------------------------------
    def add_admin(self, user_id: int, added_by: int) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
                    (user_id, added_by),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось добавить админа: %s", exc)

    def remove_admin(self, user_id: int) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось удалить админа: %s", exc)

    def all_admins(self) -> list[int]:
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute("SELECT user_id FROM admins")
                return [row["user_id"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить админов: %s", exc)
            return []

    def is_admin(self, user_id: int, super_admin_id: int) -> bool:
        """Супер-админ (из .env) или добавленный через /give_admin."""
        if user_id == super_admin_id:
            return True
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    #  Админка (5.4): здоровье каналов
    # ------------------------------------------------------------------
    def channel_seen(self, channel: str, module: str) -> None:
        """Отметить, что из канала пришло сообщение (обновить last_seen)."""
        try:
            with self._lock:
                assert self._conn is not None
                now = int(time.time())
                self._conn.execute(
                    "INSERT INTO channel_health (channel, module, last_seen, error_count, disabled) "
                    "VALUES (?, ?, ?, 0, 0) "
                    "ON CONFLICT(channel) DO UPDATE SET last_seen=excluded.last_seen",
                    (channel, module, now),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось отметить канал активным: %s", exc)

    def channel_error(self, channel: str, module: str, error: str) -> None:
        """Записать ошибку парсинга канала (увеличивает счётчик)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO channel_health (channel, module, error_count, last_error, disabled) "
                    "VALUES (?, ?, 1, ?, 0) "
                    "ON CONFLICT(channel) DO UPDATE SET "
                    "error_count=channel_health.error_count+1, last_error=excluded.last_error",
                    (channel, module, error[:300]),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать ошибку канала: %s", exc)

    def disable_channel(self, channel: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "UPDATE channel_health SET disabled=1 WHERE channel=?", (channel,)
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось забанить канал: %s", exc)

    def enable_channel(self, channel: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "UPDATE channel_health SET disabled=0, error_count=0, last_error=NULL WHERE channel=?",
                    (channel,),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось разбанить канал: %s", exc)

    def all_channel_health(self) -> list[dict]:
        """Состояние всех каналов (для /status)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT channel, module, last_seen, error_count, last_error, disabled "
                    "FROM channel_health ORDER BY module, channel"
                )
                return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить здоровье каналов: %s", exc)
            return []
