"""database.py — SQLite-журнал угроз и тревог.

Хранит каждую обработанную угрозу (время, тип, регион, исходный текст,
источник) и события тревог (регион, начало, конец). На этой истории
строятся статистика и ETA (eta.py).

БД — один файл ``airradar.db`` (см. DB_PATH в config). Используется
``check_same_thread=False`` + ``sqlite3`` синхронно: записи редкие (по факту
событий), блокировки не страшны. Все вызовы обёрнуты try/except, чтобы
ошибка БД не роняла конвейер бота.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

# Срок хранения записей в днях (старше автоматически подчищается).
RETENTION_DAYS = 60


class Database:
    """Тонкий слой над SQLite для журнала угроз и тревог."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        # Один объект соединения, защищённый локом (Telethon вызывает из
        # разных тасок event loop, но sqlite3-connection не thread-safe
        # без блокировки).
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        # Окно дедупликации (сек) для is_duplicate_persistent. По умолчанию 1 час.
        self._dedup_window = 3600
        self._connect()
        self._init_schema()

    # ------------------------------------------------------------------
    #  Подключение и схема
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def _init_schema(self) -> None:
        """Создать таблицы, если их ещё нет."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.cursor()
            # Журнал угроз: каждая обработанная и опубликованная угроза.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS threats (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,        -- unix time события
                    threat_type TEXT    NOT NULL,        -- 'missile'|'uav'|'explosion'|'stand_down'|'other'
                    region      TEXT    NOT NULL,        -- slug региона (REGIONS)
                    text        TEXT    NOT NULL,        -- сжатый/исходный текст
                    source      TEXT    NOT NULL DEFAULT ''
                )
                """
            )
            # Журнал тревог: пары старт→отбой по регионам.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    region      TEXT    NOT NULL,
                    started_ts  INTEGER NOT NULL,
                    ended_ts    INTEGER               -- NULL = тревога ещё активна
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_threats_region ON threats(region)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_threats_ts ON threats(ts)")
            # Журнал дедупликации: отпечатки текстов для отсева повторов
            # между каналами (переживает рестарт, окно до часа и больше).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_hashes (
                    fp    TEXT PRIMARY KEY,   -- sha256 нормализованного текста
                    ts    INTEGER NOT NULL    -- когда впервые увиден
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_hashes(ts)")
            # Подписки пользователей на регионы (для рассылки в ЛС).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id   INTEGER NOT NULL,   -- Telegram user id подписчика
                    region    TEXT    NOT NULL,   -- slug региона
                    PRIMARY KEY (user_id, region)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_region ON subscriptions(region)")

            # --- Interests-модуль ---
            # User-каналы: какие новостные каналы добавил каждый пользователь.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_channels (
                    user_id    INTEGER NOT NULL,
                    channel    TEXT    NOT NULL,    -- @username или -100...
                    PRIMARY KEY (user_id, channel)
                )
                """
            )
            # Подписки на темы (Interests-модуль).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_topics (
                    user_id  INTEGER NOT NULL,
                    topic    TEXT    NOT NULL,      -- slug темы
                    PRIMARY KEY (user_id, topic)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usertopics_topic ON user_topics(topic)")
            # Журнал классифицированных постов (для дайджестов/аналитики на следующих этапах).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS classified_posts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       INTEGER NOT NULL,
                    source   TEXT    NOT NULL,
                    text     TEXT    NOT NULL,
                    topics   TEXT    NOT NULL        -- slug-ы через запятую
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cposts_ts ON classified_posts(ts)")
            self._conn.commit()
        logger.debug("БД инициализирована: %s", self._path)

    # ------------------------------------------------------------------
    #  Запись
    # ------------------------------------------------------------------
    def add_threat(
        self, threat_type: str, region: str, text: str, source: str = ""
    ) -> None:
        """Записать одну угрозу в журнал. Ошибки логируются, не роняют бот."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO threats (ts, threat_type, region, text, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(time.time()), threat_type, region, text, source),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать угрозу в БД: %s", exc)

    def alert_start(self, region: str) -> None:
        """Отметить начало тревоги в регионе (если ещё нет активной)."""
        try:
            with self._lock:
                assert self._conn is not None
                # Не создаём дубль, если в регионе уже есть незакрытая тревога.
                row = self._conn.execute(
                    "SELECT id FROM alerts WHERE region = ? AND ended_ts IS NULL",
                    (region,),
                ).fetchone()
                if row is not None:
                    return  # уже активна
                self._conn.execute(
                    "INSERT INTO alerts (region, started_ts) VALUES (?, ?)",
                    (region, int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать старт тревоги: %s", exc)

    def alert_end(self, region: str) -> None:
        """Закрыть активную тревогу в регионе (поставить ended_ts)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "UPDATE alerts SET ended_ts = ? "
                    "WHERE region = ? AND ended_ts IS NULL",
                    (int(time.time()), region),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось закрыть тревогу: %s", exc)

    # ------------------------------------------------------------------
    #  Персистентная дедупликация (отсев повторов между каналами)
    # ------------------------------------------------------------------
    def is_duplicate_persistent(self, fingerprint: str) -> bool:
        """Проверить+запомнить отпечаток в SQLite. True = уже видели.

        В отличие от in-memory кеша, переживает рестарт бота и хранит
        отпечатки дольше (часы), что важно: каналы репостят друг друга
        с разницей до получаса.
        """
        try:
            now = int(time.time())
            with self._lock:
                assert self._conn is not None
                # Убираем протухшие (старше окна дедупликации).
                cutoff = now - self._dedup_window
                self._conn.execute("DELETE FROM seen_hashes WHERE ts < ?", (cutoff,))
                # Есть ли отпечаток?
                row = self._conn.execute(
                    "SELECT 1 FROM seen_hashes WHERE fp = ?", (fingerprint,)
                ).fetchone()
                if row is not None:
                    return True  # уже видели — дубликат
                # Запоминаем.
                self._conn.execute(
                    "INSERT OR REPLACE INTO seen_hashes (fp, ts) VALUES (?, ?)",
                    (fingerprint, now),
                )
                self._conn.commit()
                return False
        except sqlite3.Error as exc:
            logger.warning("Дедупликация (БД): ошибка %s — пропускаем проверку", exc)
            return False  # при ошибке БД лучше пропустить, чем блокировать

    def set_dedup_window(self, seconds: int) -> None:
        """Установить окно дедупликации (сек). По умолчанию 1 час."""
        self._dedup_window = seconds

    # ------------------------------------------------------------------
    #  Чтение: статистика и ETA
    # ------------------------------------------------------------------
    def threat_counts(self, region: str | None = None, since: float = 0.0) -> dict[str, int]:
        """Счётчики по типам угроз (missile/uav/explosion/...).

        region: None = по всем регионам. since: unix time, от которого считать.
        """
        try:
            with self._lock:
                assert self._conn is not None
                if region is None:
                    cur = self._conn.execute(
                        "SELECT threat_type, COUNT(*) c FROM threats WHERE ts >= ? "
                        "GROUP BY threat_type",
                        (int(since),),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT threat_type, COUNT(*) c FROM threats "
                        "WHERE region = ? AND ts >= ? GROUP BY threat_type",
                        (region, int(since)),
                    )
                return {row["threat_type"]: row["c"] for row in cur.fetchall()}
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать счётчики угроз: %s", exc)
            return {}

    def recent_threats(self, region: str, limit: int = 5) -> list[dict[str, Any]]:
        """Последние N угроз в регионе (для «История ударов»)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT ts, threat_type, text FROM threats "
                    "WHERE region = ? ORDER BY ts DESC LIMIT ?",
                    (region, limit),
                )
                return [
                    {"ts": row["ts"], "type": row["threat_type"], "text": row["text"]}
                    for row in cur.fetchall()
                ]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать историю угроз: %s", exc)
            return []

    def active_threats(self, within_seconds: int = 1800) -> list[dict[str, Any]]:
        """Активные угрозы за последние N секунд (по умолчанию 30 мин)."""
        try:
            cutoff = int(time.time()) - within_seconds
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT ts, threat_type, region, text FROM threats "
                    "WHERE ts >= ? ORDER BY ts DESC",
                    (cutoff,),
                )
                return [
                    {
                        "ts": row["ts"],
                        "type": row["threat_type"],
                        "region": row["region"],
                        "text": row["text"],
                    }
                    for row in cur.fetchall()
                ]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать активные угрозы: %s", exc)
            return []

    def avg_alert_duration(self, region: str) -> float | None:
        """Средняя длительность тревоги в регионе (сек), или None если данных мало."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT AVG(ended_ts - started_ts) a FROM alerts "
                    "WHERE region = ? AND ended_ts IS NOT NULL",
                    (region,),
                )
                row = cur.fetchone()
                return row["a"] if row and row["a"] is not None else None
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать среднюю тревогу: %s", exc)
            return None

    def active_alert_regions(self) -> list[str]:
        """Список регионов с активной (незакрытой) тревогой прямо сейчас."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT region FROM alerts WHERE ended_ts IS NULL"
                )
                return [row["region"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать активные тревоги: %s", exc)
            return []

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

    def subscribe_topic(self, user_id: int, topic: str) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_topics (user_id, topic) VALUES (?, ?)",
                    (user_id, topic),
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

    def topic_subscribers(self, topic: str) -> list[int]:
        """user_id всех подписчиков темы (для рассылки)."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT user_id FROM user_topics WHERE topic = ?", (topic,)
                )
                return [row["user_id"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить подписчиков темы: %s", exc)
            return []

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

    def save_classification(self, source: str, text: str, topics: str) -> None:
        """Записать классифицированный пост в журнал."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO classified_posts (ts, source, text, topics) VALUES (?, ?, ?, ?)",
                    (int(time.time()), source, text[:1000], topics),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить классификацию: %s", exc)

    # ------------------------------------------------------------------
    #  Очистка
    # ------------------------------------------------------------------
    def cleanup(self) -> int:
        """Удалить записи старше RETENTION_DAYS. Возвращает число удалённых."""
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute("DELETE FROM threats WHERE ts < ?", (cutoff,))
                cur2 = self._conn.execute("DELETE FROM alerts WHERE ended_ts IS NOT NULL AND ended_ts < ?", (cutoff,))
                self._conn.commit()
                return cur.rowcount + cur2.rowcount
        except sqlite3.Error as exc:
            logger.warning("Не удалось очистить БД: %s", exc)
            return 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
