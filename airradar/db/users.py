"""Mixin Database: подписки и настройки пользователей."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


class UsersMixin:
    """подписки и настройки пользователей. Использует self._lock и self._conn из Database."""


    # ------------------------------------------------------------------
    #  Подписки на регионы (рассылка в ЛС)
    # ------------------------------------------------------------------
    def subscribe(self, user_id: int, region: str) -> None:
        """Подписать пользователя на регион (idempotent)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO subscriptions (user_id, region) VALUES (?, ?)",
                    (user_id, region),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось подписать: %s", exc)


    def unsubscribe(self, user_id: int, region: str) -> None:
        """Отписать пользователя от региона."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "DELETE FROM subscriptions WHERE user_id = ? AND region = ?",
                    (user_id, region),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось отписать: %s", exc)


    def is_subscribed(self, user_id: int, region: str) -> bool:
        """Проверить, подписан ли пользователь на регион."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM subscriptions WHERE user_id = ? AND region = ?",
                    (user_id, region),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            logger.warning("Не удалось проверить подписку: %s", exc)
            return False


    def get_subscribers(self, region: str) -> list[int]:
        """Список user_id, подписанных на регион (для рассылки)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT user_id FROM subscriptions WHERE region = ?", (region,)
                )
                return [row["user_id"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить подписчиков: %s", exc)
            return []


    def user_subscriptions(self, user_id: int) -> list[str]:
        """Список регионов, на которые подписан пользователь (для меню)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT region FROM subscriptions WHERE user_id = ?", (user_id,)
                )
                return [row["region"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить подписки: %s", exc)
            return []


    def get_notify_levels(self, user_id: int) -> set[str]:
        """Типи сповіщень, які користувач ВИМКНУВ ({'info','analytic','high'}); critical вимкнути не можна."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("CREATE TABLE IF NOT EXISTS notify_off (user_id INTEGER NOT NULL, level TEXT NOT NULL, PRIMARY KEY(user_id, level))")
                rows = self._conn.execute("SELECT level FROM notify_off WHERE user_id = ?", (user_id,)).fetchall()
            return {r["level"] for r in rows}
        except sqlite3.Error:
            return set()


    def toggle_notify_level(self, user_id: int, level: str) -> bool:
        """Перемкнути рівень; повертає False для critical/невідомого рівня."""
        if level not in ("high", "info", "analytic"):
            return False
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("CREATE TABLE IF NOT EXISTS notify_off (user_id INTEGER NOT NULL, level TEXT NOT NULL, PRIMARY KEY(user_id, level))")
                cur = self._conn.execute("DELETE FROM notify_off WHERE user_id = ? AND level = ?", (user_id, level))
                if cur.rowcount == 0:
                    self._conn.execute("INSERT INTO notify_off (user_id, level) VALUES (?, ?)", (user_id, level))
                self._conn.commit()
            return True
        except sqlite3.Error:
            return False


    def get_night_mode(self, user_id: int) -> bool:
        """Включён ли ночной режим у пользователя."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT night_mode FROM user_prefs WHERE user_id = ?", (user_id,)
                ).fetchone()
                return bool(row["night_mode"]) if row else False
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать night_mode: %s", exc)
            return False


    def set_night_mode(self, user_id: int, enabled: bool) -> None:
        """Включить/выключить ночной режим (23:00–06:00 только критичные)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO user_prefs (user_id, night_mode) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET night_mode = ?",
                    (user_id, 1 if enabled else 0, 1 if enabled else 0),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить night_mode: %s", exc)


    def get_shelter_mode(self, user_id: int) -> bool:
        """Режим «Укриття»: доставлять только тревогу (критичные) и відбій."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT shelter_mode FROM user_prefs WHERE user_id = ?", (user_id,)
                ).fetchone()
                return bool(row["shelter_mode"]) if row else False
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать shelter_mode: %s", exc)
            return False


    def set_shelter_mode(self, user_id: int, enabled: bool) -> None:
        """Включить/выключить режим «Укриття» (одно сообщение на тревогу/відбій)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO user_prefs (user_id, shelter_mode) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET shelter_mode = ?",
                    (user_id, 1 if enabled else 0, 1 if enabled else 0),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить shelter_mode: %s", exc)


    def get_home_region(self, user_id: int) -> str:
        """Slug «Моєї зони» (город/район/область по геопозиции); '' — не задана."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT home_region FROM user_prefs WHERE user_id = ?", (user_id,)
                ).fetchone()
                return str(row["home_region"] or "") if row else ""
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать home_region: %s", exc)
            return ""


    def set_home_region(self, user_id: int, slug: str) -> None:
        """Сохранить «Мою зону» по геопозиции ('' — сбросить)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO user_prefs (user_id, home_region) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET home_region = ?",
                    (user_id, slug or "", slug or ""),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить home_region: %s", exc)


    def get_weapon_classes(self, user_id: int) -> str:
        """CSV разрешённых групп ('' = без фильтра — приходят все классы)."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT weapon_classes FROM user_prefs WHERE user_id = ?", (user_id,)
                ).fetchone()
                return str(row["weapon_classes"] or "") if row else ""
        except sqlite3.Error:
            return ""


    def set_weapon_classes(self, user_id: int, groups_csv: str) -> None:
        """Сохранить CSV разрешённых групп ('' = сбросить фильтр)."""
        allowed = set(self.WEAPON_GROUPS)
        parts = [p.strip() for p in (groups_csv or "").split(",") if p.strip()]
        clean = ",".join(p for p in parts if p in allowed)
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO user_prefs (user_id, weapon_classes) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET weapon_classes = ?",
                    (user_id, clean, clean),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить weapon_classes: %s", exc)


    # ------------------------------------------------------------------
    #  Interests-модуль: каналы, темы, классификация
    # ------------------------------------------------------------------
    def add_user_channel(self, user_id: int, channel: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_channels (user_id, channel) VALUES (?, ?)",
                    (user_id, channel),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось добавить канал: %s", exc)


    def remove_user_channel(self, user_id: int, channel: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "DELETE FROM user_channels WHERE user_id = ? AND channel = ?",
                    (user_id, channel),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось удалить канал: %s", exc)


    def all_interests_channels(self) -> list[str]:
        """Все уникальные каналы, добавленные любым пользователем (для парсинга)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute("SELECT DISTINCT channel FROM user_channels")
                return [row["channel"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить каналы: %s", exc)
            return []


    def user_channels(self, user_id: int) -> list[str]:
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT channel FROM user_channels WHERE user_id = ?", (user_id,)
                )
                return [row["channel"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить каналы пользователя: %s", exc)
            return []


    def subscribe_topic(self, user_id: int, topic: str, mode: str = "instant") -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO user_topics (user_id, topic, delivery_mode) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, topic) DO UPDATE SET delivery_mode=excluded.delivery_mode",
                    (user_id, topic, mode),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось подписать на тему: %s", exc)


    def unsubscribe_topic(self, user_id: int, topic: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "DELETE FROM user_topics WHERE user_id = ? AND topic = ?",
                    (user_id, topic),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось отписать от темы: %s", exc)


    def is_subscribed_topic(self, user_id: int, topic: str) -> bool:
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM user_topics WHERE user_id = ? AND topic = ?",
                    (user_id, topic),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            logger.warning("Не удалось проверить подписку темы: %s", exc)
            return False


    def topic_subscribers(self, topic: str, mode: str | None = None) -> list[int]:
        """user_id подписчиков темы. mode='instant'|'digest' — фильтр (None=все)."""
        try:
            with self._lock:
                assert self._conn is not None
                if mode is None:
                    cur = self._conn.execute(
                        "SELECT user_id FROM user_topics WHERE topic = ?", (topic,)
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT user_id FROM user_topics WHERE topic = ? AND delivery_mode = ?",
                        (topic, mode),
                    )
                return [row["user_id"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить подписчиков темы: %s", exc)
            return []


    def digest_subscribers_by_topic(self, topic: str) -> list[int]:
        """user_id подписчиков темы в режиме дайджеста."""
        return self.topic_subscribers(topic, mode="digest")


    def user_topics(self, user_id: int) -> list[str]:
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT topic FROM user_topics WHERE user_id = ?", (user_id,)
                )
                return [row["topic"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить темы пользователя: %s", exc)
            return []


    def user_topics_with_modes(self, user_id: int) -> dict:
        """{slug: mode} для всех тем, на которые подписан юзер."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT topic, delivery_mode FROM user_topics WHERE user_id = ?",
                    (user_id,),
                )
                return {row["topic"]: row["delivery_mode"] for row in cur.fetchall()}
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить темы+режимы: %s", exc)
            return {}


    def topic_delivery_mode(self, user_id: int, topic: str) -> str:
        """Режим выдачи юзера по теме ('instant' | 'digest')."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT delivery_mode FROM user_topics WHERE user_id=? AND topic=?",
                    (user_id, topic),
                ).fetchone()
                return row["delivery_mode"] if row else "instant"
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить режим выдачи: %s", exc)
            return "instant"


    # ------------------------------------------------------------------
    #  Семантический поиск: свободные интересы (ТЗ 5.3)
    # ------------------------------------------------------------------
    def add_interest(self, user_id: int, interest: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_interests (user_id, interest) VALUES (?, ?)",
                    (user_id, interest.strip()[:200]),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось добавить интерес: %s", exc)


    def remove_interest(self, user_id: int, interest: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "DELETE FROM user_interests WHERE user_id = ? AND interest = ?",
                    (user_id, interest),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось удалить интерес: %s", exc)


    def user_interests(self, user_id: int) -> list[str]:
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT interest FROM user_interests WHERE user_id = ?", (user_id,)
                )
                return [row["interest"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить интересы: %s", exc)
            return []


    def all_interests(self) -> list[tuple[int, str]]:
        """Все интересы всех юзеров: [(user_id, interest), ...]. Для конвейера."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute("SELECT user_id, interest FROM user_interests")
                return [(row["user_id"], row["interest"]) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить все интересы: %s", exc)
            return []
