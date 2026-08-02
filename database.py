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
