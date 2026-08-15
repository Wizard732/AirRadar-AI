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

# Исторические события нужны для ETA/вероятностей; не удаляем их по времени.
# Операционная очистка касается только дедупликации, а не фактов угроз.
RETENTION_DAYS = 0


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
            # Нормализованный слой для динамических ETA и вероятности исхода.
            # Отдельная таблица сохраняет совместимость со старым threats.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS threat_events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_ts     INTEGER NOT NULL,
                    ingested_ts  INTEGER NOT NULL,
                    weapon_class TEXT NOT NULL,
                    stage        TEXT NOT NULL,
                    region       TEXT NOT NULL,
                    direction    TEXT NOT NULL DEFAULT '',
                    outcome      TEXT NOT NULL DEFAULT 'unknown',
                    confidence   REAL NOT NULL DEFAULT 0.5,
                    source       TEXT NOT NULL DEFAULT '',
                    fingerprint  TEXT NOT NULL UNIQUE,
                    text         TEXT NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_match ON threat_events(region, weapon_class, stage, event_ts)")
            # Состояние синхронизации истории Telegram. Курсор хранится отдельно
            # для каждого канала, а message_key исключает дубли при перекрытии.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS history_sync_messages (
                    source      TEXT NOT NULL,
                    message_id  INTEGER NOT NULL,
                    event_ts    INTEGER NOT NULL,
                    PRIMARY KEY (source, message_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS history_sync_cursors (
                    source           TEXT PRIMARY KEY,
                    last_message_id  INTEGER NOT NULL DEFAULT 0,
                    last_event_ts    INTEGER NOT NULL DEFAULT 0,
                    synced_ts        INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Инциденты объединяют независимые сообщения об одной угрозе.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_key TEXT NOT NULL UNIQUE,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    weapon_class TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    region TEXT NOT NULL,
                    source_count INTEGER NOT NULL DEFAULT 0,
                    sources TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unconfirmed'
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_incidents_recent ON incidents(region, weapon_class, updated_ts)")
            for column, definition in (
                ("origin_region", "TEXT NOT NULL DEFAULT ''"),
                ("destination_region", "TEXT NOT NULL DEFAULT ''"),
                ("count_kind", "TEXT NOT NULL DEFAULT 'unspecified'"),
                ("count_value", "INTEGER"),
                ("weapon_raw", "TEXT NOT NULL DEFAULT ''"),
                ("state", "TEXT NOT NULL DEFAULT 'open'"),
                ("published_chat_id", "TEXT NOT NULL DEFAULT ''"),
                ("published_message_id", "INTEGER"),
            ):
                try:
                    cur.execute(f"ALTER TABLE incidents ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            # Доказательства и аудит отделены от старых таблиц: миграция не
            # меняет их смысл и даёт проверяемую историю решений.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_key TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_group TEXT NOT NULL,
                    text TEXT NOT NULL,
                    model_summary TEXT NOT NULL DEFAULT '',
                    created_ts INTEGER NOT NULL
                )
                """
            )
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique ON incident_evidence(incident_key, source, text)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_ts INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    raw_output TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_ts INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_key TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reviewer_id INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL
                )
                """
            )
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
                    user_id        INTEGER NOT NULL,
                    topic          TEXT    NOT NULL,      -- slug темы
                    delivery_mode  TEXT    NOT NULL DEFAULT 'instant',  -- 'instant' | 'digest'
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
            # Здоровье каналов и заблокированные источники (админка 5.4).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_health (
                    channel      TEXT PRIMARY KEY,   -- @username или -100...
                    module       TEXT NOT NULL,      -- 'military' | 'interests'
                    last_seen    INTEGER,            -- unix time последнего сообщения
                    error_count  INTEGER NOT NULL DEFAULT 0,
                    last_error   TEXT,               -- текст последней ошибки
                    disabled     INTEGER NOT NULL DEFAULT 0  -- 1 = забанен админом
                )
                """
            )
            # Супер-админ (ADMIN_ID из .env) может выдавать/забирать админку.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id  INTEGER PRIMARY KEY,
                    added_by INTEGER
                )
                """
            )
            # Структурированные сущности из постов (для детальной статистики).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS threat_entities (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       INTEGER NOT NULL,
                    region   TEXT    NOT NULL DEFAULT '',
                    weapon   TEXT    NOT NULL DEFAULT '',
                    city     TEXT    NOT NULL DEFAULT '',
                    target   TEXT    NOT NULL DEFAULT '',
                    impact   TEXT    NOT NULL DEFAULT '',
                    ppo      TEXT    NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ent_ts ON threat_entities(ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ent_region ON threat_entities(region)")
            # Журнал отправленных сводок (защита от спама при рестартах).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS digest_log (
                    key       TEXT PRIMARY KEY,   -- 'evening_2026-08-02' и т.п.
                    ts        INTEGER NOT NULL
                )
                """
            )
            # Свободные интересы юзера (семантический поиск, ТЗ 5.3).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_interests (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id  INTEGER NOT NULL,
                    interest TEXT    NOT NULL,   -- свободный текст: «фьюжн-реакторы»
                    UNIQUE (user_id, interest)
                )
                """
            )
            self._conn.commit()
        logger.debug("БД инициализирована: %s", self._path)

    # ------------------------------------------------------------------
    #  Запись
    # ------------------------------------------------------------------
    def add_threat(
        self, threat_type: str, region: str, text: str, source: str = "", *,
        event_ts: int | None = None, commit: bool = True,
    ) -> None:
        """Записать угрозу, сохраняя время исходного сообщения при наличии."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO threats (ts, threat_type, region, text, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (event_ts if event_ts is not None else int(time.time()), threat_type, region, text, source),
                )
                if commit:
                    self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать угрозу в БД: %s", exc)

    def claim_history_message(self, source: str, message_id: int, event_ts: int) -> bool:
        """Пометить Telegram-сообщение обработанным; True только при первом импорте."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO history_sync_messages (source, message_id, event_ts) VALUES (?, ?, ?)",
                    (source, message_id, event_ts),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить ключ сообщения истории: %s", exc)
            return False

    def update_history_cursor(self, source: str, message_id: int, event_ts: int) -> None:
        """Продвинуть курсор канала после успешной обработки сообщения."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    """INSERT INTO history_sync_cursors (source, last_message_id, last_event_ts, synced_ts)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(source) DO UPDATE SET
                         last_message_id=MAX(last_message_id, excluded.last_message_id),
                         last_event_ts=MAX(last_event_ts, excluded.last_event_ts),
                         synced_ts=excluded.synced_ts""",
                    (source, message_id, event_ts, int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось обновить курсор истории: %s", exc)

    def get_history_cursor(self, source: str) -> tuple[int, int]:
        """Вернуть (последний Telegram ID, timestamp) для канала."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT last_message_id, last_event_ts FROM history_sync_cursors WHERE source=?", (source,)
                ).fetchone()
                return (int(row["last_message_id"]), int(row["last_event_ts"])) if row else (0, 0)
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать курсор истории: %s", exc)
            return (0, 0)

    def get_history_reconcile_min_id(self, source: str, cutoff_ts: int) -> int:
        """Вернуть первый ID уже прочитанного сообщения в окне сверки."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT MIN(message_id) AS message_id FROM history_sync_messages WHERE source=? AND event_ts>=?",
                    (source, cutoff_ts),
                ).fetchone()
                return int(row["message_id"] or 0)
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать окно сверки истории: %s", exc)
            return 0

    def add_event(
        self,
        *,
        event_ts: int,
        weapon_class: str,
        stage: str,
        region: str,
        text: str,
        source: str = "",
        direction: str = "",
        outcome: str = "unknown",
        confidence: float = 0.7,
        commit: bool = True,
    ) -> bool:
        """Сохранить нормализованное событие идемпотентно.

        Возвращает True только для новой записи. fingerprint защищает историю
        от повторного импорта и репостов с тем же очищенным текстом/временем.
        """
        import hashlib
        fingerprint = hashlib.sha256(
            f"{event_ts}|{source}|{region}|{weapon_class}|{stage}|{text.lower()}".encode("utf-8")
        ).hexdigest()
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO threat_events "
                    "(event_ts, ingested_ts, weapon_class, stage, region, direction, outcome, confidence, source, fingerprint, text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_ts, int(time.time()), weapon_class, stage, region, direction, outcome,
                     max(0.0, min(1.0, confidence)), source, fingerprint, text[:700]),
                )
                if commit:
                    self._conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать нормализованное событие: %s", exc)
            return False

    def register_incident(
        self, *, event_ts: int, weapon_class: str, stage: str, region: str, source: str,
        source_group: str = "", official: bool = False, window_seconds: int = 1200,
        confirmation_sources: int = 2, text: str = "", model_summary: str = "",
    ) -> dict[str, Any]:
        """Создать/обновить инцидент и вернуть независимые подтверждения.

        Сообщения в 20-минутном окне с одинаковым оружием/стадией/регионом
        считаются одним инцидентом. Один источник учитывается лишь один раз.
        """
        bucket = event_ts // max(60, window_seconds)
        key = f"{bucket}:{region}:{weapon_class}:{stage}"
        source_group = source_group or source
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute("SELECT * FROM incidents WHERE incident_key = ?", (key,)).fetchone()
                if row is None:
                    groups = [source_group] if source_group else []
                    status = "officially_confirmed" if official else "reported"
                    self._conn.execute(
                        "INSERT INTO incidents (incident_key, created_ts, updated_ts, weapon_class, stage, region, source_count, sources, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, event_ts, event_ts, weapon_class, stage, region, len(groups), ",".join(groups), status),
                    )
                else:
                    groups = [item for item in row["sources"].split(",") if item]
                    if source_group and source_group not in groups:
                        groups.append(source_group)
                    if official or row["status"] == "officially_confirmed":
                        status = "officially_confirmed"
                    elif len(groups) >= confirmation_sources:
                        status = "corroborated"
                    else:
                        status = row["status"] if row["status"] in {"disputed", "retracted"} else "reported"
                    self._conn.execute(
                        "UPDATE incidents SET updated_ts=?, source_count=?, sources=?, status=? WHERE incident_key=?",
                        (event_ts, len(groups), ",".join(groups), status, key),
                    )
                self._conn.execute(
                    "INSERT OR IGNORE INTO incident_evidence "
                    "(incident_key, event_ts, source, source_group, text, model_summary, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, event_ts, source, source_group, text[:2000], model_summary[:500], int(time.time())),
                )
                self._conn.commit()
                return {"key": key, "sources": len(groups), "status": status, "official": official}
        except sqlite3.Error as exc:
            logger.warning("Не удалось зарегистрировать инцидент: %s", exc)
            return {"key": key, "sources": 1, "status": "unconfirmed"}

    def merge_incident_fact(
        self, *, event_ts: int, source: str, source_group: str, fact, text: str,
        window_seconds: int = 1200, confirmation_sources: int = 2,
    ) -> dict[str, Any]:
        """Attach an explicit follow-up to a recent compatible open incident."""
        cutoff = event_ts - max(60, window_seconds)
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT * FROM incidents WHERE state='open' AND updated_ts>=? "
                    "AND destination_region=? AND stage=? ORDER BY updated_ts DESC",
                    (cutoff, fact.destination_region, fact.stage),
                ).fetchall()
                row = next((candidate for candidate in rows if candidate["weapon_class"] == fact.weapon_class), None)
                if row is None and fact.is_delta and fact.weapon_class == "unknown" and len(rows) == 1:
                    row = rows[0]
                if row is None:
                    key = f"{event_ts}:{fact.destination_region}:{fact.weapon_class}:{fact.stage}"
                    groups = [source_group] if source_group else []
                    status = "reported"
                    self._conn.execute(
                        "INSERT INTO incidents (incident_key, created_ts, updated_ts, weapon_class, stage, region, source_count, sources, status, origin_region, destination_region, count_kind, count_value, weapon_raw, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                        (key, event_ts, event_ts, fact.weapon_class, fact.stage, fact.destination_region,
                         len(groups), ",".join(groups), status, fact.origin_region, fact.destination_region,
                         fact.count_kind, fact.count_value, fact.raw_designation),
                    )
                    merged, old_status, old_count = False, status, None
                else:
                    key = row["incident_key"]
                    groups = [item for item in row["sources"].split(",") if item]
                    if source_group and source_group not in groups:
                        groups.append(source_group)
                    status = "corroborated" if len(groups) >= confirmation_sources else row["status"]
                    count_kind, count_value = row["count_kind"], row["count_value"]
                    if fact.count_kind == "delta" and fact.count_value is not None:
                        count_value = (count_value or 0) + fact.count_value
                        count_kind = "reported_total"
                    elif fact.count_kind == "exact" and count_value is None:
                        count_kind, count_value = "exact", fact.count_value
                    elif fact.count_kind == "exact" and count_value != fact.count_value:
                        count_kind = "conflicting"
                    self._conn.execute(
                        "UPDATE incidents SET updated_ts=?, source_count=?, sources=?, status=?, origin_region=?, count_kind=?, count_value=?, weapon_raw=? WHERE incident_key=?",
                        (event_ts, len(groups), ",".join(groups), status, fact.origin_region or row["origin_region"],
                         count_kind, count_value, fact.raw_designation or row["weapon_raw"], key),
                    )
                    merged, old_status, old_count = True, row["status"], row["count_value"]
                self._conn.execute(
                    "INSERT OR IGNORE INTO incident_evidence (incident_key, event_ts, source, source_group, text, model_summary, created_ts) VALUES (?, ?, ?, ?, ?, '', ?)",
                    (key, event_ts, source, source_group, text[:2000], int(time.time())),
                )
                self._conn.commit()
                return {"key": key, "sources": len(groups), "status": status, "merged": merged,
                        "material_update": not merged or old_status != status or old_count != fact.count_value or fact.is_delta,
                        "count_kind": count_kind if merged else fact.count_kind,
                        "count_value": count_value if merged else fact.count_value,
                        "destination_region": fact.destination_region}
        except sqlite3.Error as exc:
            logger.warning("Не удалось слить факты инцидента: %s", exc)
            return {"key": "", "sources": 1, "status": "reported", "merged": False, "material_update": False}

    def save_incident_publication(self, incident_key: str, chat_id: str, message_id: int) -> None:
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("UPDATE incidents SET published_chat_id=?, published_message_id=? WHERE incident_key=?", (chat_id, message_id, incident_key))
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить публикацию инцидента: %s", exc)

    def incident_publication(self, incident_key: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute("SELECT published_chat_id, published_message_id FROM incidents WHERE incident_key=?", (incident_key,)).fetchone()
                return dict(row) if row and row["published_message_id"] else None
        except sqlite3.Error:
            return None

    def record_inference_audit(
        self, *, event_ts: int, source: str, model: str, raw_output: str,
        decision: str, reason: str = "", prompt_version: str = "v2",
    ) -> None:
        """Сохранить объяснимое решение LLM без влияния на публикацию."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO inference_audit (event_ts, source, model, prompt_version, raw_output, decision, reason, created_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_ts, source, model, prompt_version, raw_output[:1000], decision, reason[:300], int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать аудит LLM: %s", exc)

    def review_incident(self, incident_key: str, decision: str, reason: str, reviewer_id: int) -> bool:
        """Зафиксировать решение администратора и обновить статус инцидента."""
        allowed = {"officially_confirmed", "disputed", "retracted", "resolved"}
        if decision not in allowed or not reason.strip():
            return False
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("UPDATE incidents SET status=?, updated_ts=? WHERE incident_key=?", (decision, int(time.time()), incident_key))
                self._conn.execute(
                    "INSERT INTO incident_reviews (incident_key, decision, reason, reviewer_id, created_ts) VALUES (?, ?, ?, ?, ?)",
                    (incident_key, decision, reason[:500], reviewer_id, int(time.time())),
                )
                self._conn.commit()
                return True
        except sqlite3.Error as exc:
            logger.warning("Не удалось проверить инцидент: %s", exc)
            return False

    def review_queue(self, limit: int = 10) -> list[dict[str, Any]]:
        """Неподтверждённые и спорные свежие инциденты для админ-проверки."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT incident_key, updated_ts, weapon_class, stage, region, source_count, status "
                    "FROM incidents WHERE status IN ('reported', 'disputed') ORDER BY updated_ts DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить очередь проверки: %s", exc)
            return []

    def is_channel_disabled(self, channel: str, module: str) -> bool:
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT disabled FROM channel_health WHERE channel=? AND module=?", (channel, module)
                ).fetchone()
                return bool(row and row["disabled"])
        except sqlite3.Error:
            return False

    def alert_start(self, region: str, *, event_ts: int | None = None) -> None:
        """Отметить начало тревоги в регионе (если ещё нет активной)."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT id FROM alerts WHERE region = ? AND ended_ts IS NULL",
                    (region,),
                ).fetchone()
                if row is not None:
                    return
                self._conn.execute(
                    "INSERT INTO alerts (region, started_ts) VALUES (?, ?)",
                    (region, event_ts if event_ts is not None else int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать старт тревоги: %s", exc)

    def alert_end(self, region: str, *, event_ts: int | None = None) -> None:
        """Закрыть активную тревогу в регионе (поставить ended_ts)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "UPDATE alerts SET ended_ts = ? WHERE region = ? AND ended_ts IS NULL",
                    (event_ts if event_ts is not None else int(time.time()), region),
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
        """Только свежие подтверждённые активные инциденты, не все старые посты."""
        try:
            cutoff = int(time.time()) - within_seconds
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT i.updated_ts AS ts, i.weapon_class AS type, i.region, i.status, i.source_count, "
                    "(SELECT text FROM incident_evidence e WHERE e.incident_key=i.incident_key ORDER BY e.id DESC LIMIT 1) AS text "
                    "FROM incidents i WHERE i.updated_ts >= ? AND i.stage IN ('imminent', 'potential') "
                    "AND i.status IN ('corroborated', 'officially_confirmed') ORDER BY i.updated_ts DESC",
                    (cutoff,),
                )
                return [dict(row) for row in cur.fetchall()]
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

    def recent_classified_posts(self, topic: str, since_ts: int, limit: int = 5) -> list[dict]:
        """Посты темы за период (для дайджеста). Фильтр по теме через LIKE."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT ts, source, text FROM classified_posts "
                    "WHERE ts >= ? AND (','||topics||',') LIKE ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (since_ts, f"%,{topic},%", limit),
                )
                return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить посты для дайджеста: %s", exc)
            return []

    def all_digest_topics(self) -> list[str]:
        """Уникальные темы, у которых есть хотя бы один digest-подписчик."""
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT DISTINCT topic FROM user_topics WHERE delivery_mode='digest'"
                )
                return [row["topic"] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось получить дайджест-темы: %s", exc)
            return []

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

    # ------------------------------------------------------------------
    #  Сущности: детальная статистика (города, объекты, последствия)
    # ------------------------------------------------------------------
    def save_entities(self, region: str, entities: dict[str, str]) -> None:
        """Сохранить извлечённые сущности поста."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO threat_entities (ts, region, weapon, city, target, impact, ppo) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        region,
                        entities.get("weapon", ""),
                        entities.get("city", ""),
                        entities.get("target", ""),
                        entities.get("impact", ""),
                        entities.get("ppo", ""),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("save_entities: %s", exc)

    def entity_counts(self, field: str, region: str | None = None, since: float = 0.0) -> dict[str, int]:
        """Топ-значения одного поля (weapon/city/target/impact/ppo) за период."""
        allowed = {"weapon", "city", "target", "impact", "ppo"}
        if field not in allowed:
            return {}
        try:
            with self._lock:
                assert self._conn is not None
                if region:
                    cur = self._conn.execute(
                        f"SELECT {field} f, COUNT(*) c FROM threat_entities "
                        f"WHERE {field} != '' AND region = ? AND ts >= ? GROUP BY {field} ORDER BY c DESC LIMIT 15",
                        (region, int(since)),
                    )
                else:
                    cur = self._conn.execute(
                        f"SELECT {field} f, COUNT(*) c FROM threat_entities "
                        f"WHERE {field} != '' AND ts >= ? GROUP BY {field} ORDER BY c DESC LIMIT 15",
                        (int(since),),
                    )
                return {row["f"]: row["c"] for row in cur.fetchall()}
        except sqlite3.Error as exc:
            logger.warning("entity_counts: %s", exc)
            return {}

    def eta_per_weapon(self, region: str | None = None) -> dict[str, int]:
        """Средний ETA по типам оружия (из threat_entities weapon + threats)."""
        # Упрощённо: берём weapon из threat_entities, сопоставляем с ETA из threats.
        # Возвращаем {weapon: avg_minutes}.
        try:
            with self._lock:
                assert self._conn is not None
                # Связываем по времени: для каждого weapon берём пары пуск→прилёт.
                cur = self._conn.execute(
                    "SELECT weapon, COUNT(*) c FROM threat_entities "
                    "WHERE weapon != '' GROUP BY weapon ORDER BY c DESC LIMIT 10"
                )
                weapons = {row["weapon"]: row["c"] for row in cur.fetchall()}
                # ETA по типам оружия пока грубо: из threats (missile/uav).
                return weapons  # {weapon: count}
        except sqlite3.Error as exc:
            logger.warning("eta_per_weapon: %s", exc)
            return {}

    # ------------------------------------------------------------------
    #  Прогнозная аналитика: паттерны по часам, корреляции, коридоры
    # ------------------------------------------------------------------
    def hourly_pattern(self, region: str, threat_type: str | None = None) -> list[int]:
        """Распределение угроз по часам (0-23) для региона.

        Возвращает список из 24 чисел — сколько угроз пришлось на каждый час
        за весь архив. Нужен для прогноза «сейчас исторически активный час».
        """
        try:
            with self._lock:
                assert self._conn is not None
                if threat_type:
                    cur = self._conn.execute(
                        "SELECT ts FROM threats WHERE region=? AND threat_type=?",
                        (region, threat_type),
                    )
                else:
                    cur = self._conn.execute(
                        "SELECT ts FROM threats WHERE region=?", (region,)
                    )
                hours = [0] * 24
                for row in cur:
                    h = time.localtime(row["ts"]).tm_hour
                    hours[h] += 1
                return hours
        except sqlite3.Error as exc:
            logger.warning("hourly_pattern: %s", exc)
            return [0] * 24

    def avg_time_between(self, region: str, trigger_type: str, follow_type: str, window: int = 14400) -> float | None:
        """Среднее время между триггером (напр. зліт Ту-95) и последующей угрозой.

        Ищет пары: запись trigger_type → запись follow_type в том же регионе
        в течение window секунд. Возвращает среднее время в секундах или None.
        Нужен для корреляции «авиация → удар».
        """
        try:
            with self._lock:
                assert self._conn is not None
                triggers = self._conn.execute(
                    "SELECT ts FROM threats WHERE region=? AND threat_type=? ORDER BY ts",
                    (region, trigger_type),
                ).fetchall()
                follows = self._conn.execute(
                    "SELECT ts FROM threats WHERE region=? AND threat_type=? ORDER BY ts",
                    (region, follow_type),
                ).fetchall()
                follow_ts = [r["ts"] for r in follows]
                pairs: list[int] = []
                for trig in triggers:
                    tts = trig["ts"]
                    for fts in follow_ts:
                        delta = fts - tts
                        if 0 < delta <= window:
                            pairs.append(delta)
                            break
                if len(pairs) < 2:
                    return None
                return sum(pairs) / len(pairs)
        except sqlite3.Error as exc:
            logger.warning("avg_time_between: %s", exc)
            return None

    def alert_duration_samples(self, region: str, limit: int = 50) -> list[int]:
        """Список длительностей завершённых тревог в регионе (для коридора укрытия).

        Возвращает список секунд. По нему считаем медиану и разброс
        «сколько сидеть в укрытии».
        """
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT ended_ts - started_ts AS dur FROM alerts "
                    "WHERE region=? AND ended_ts IS NOT NULL "
                    "ORDER BY started_ts DESC LIMIT ?",
                    (region, limit),
                )
                return [row["dur"] for row in cur if row["dur"] and row["dur"] > 0]
        except sqlite3.Error as exc:
            logger.warning("alert_duration_samples: %s", exc)
            return []

    def threats_in_last_hours(self, region: str, hours: int = 3) -> int:
        """Сколько угроз было в регионе за последние N часов (индикатор активности)."""
        try:
            cutoff = int(time.time()) - hours * 3600
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM threats WHERE region=? AND ts >= ?",
                    (region, cutoff),
                ).fetchone()
                return row["c"] if row else 0
        except sqlite3.Error as exc:
            logger.warning("threats_in_last_hours: %s", exc)
            return 0

    # ------------------------------------------------------------------
    #  Журнал сводок (защита от спама при рестартах)
    # ------------------------------------------------------------------
    def is_digest_sent(self, key: str) -> bool:
        """Проверить, была ли уже отправлена сводка с этим ключом сегодня."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM digest_log WHERE key = ?", (key,)
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def mark_digest_sent(self, key: str) -> None:
        """Отметить сводку как отправленную."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR REPLACE INTO digest_log (key, ts) VALUES (?, ?)",
                    (key, int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error:
            pass

    def cleanup_digest_log(self) -> None:
        """Удалить записи старше 7 дней."""
        cutoff = int(time.time()) - 7 * 86400
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("DELETE FROM digest_log WHERE ts < ?", (cutoff,))
                self._conn.commit()
        except sqlite3.Error:
            pass

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

    # ------------------------------------------------------------------
    #  Очистка
    # ------------------------------------------------------------------
    def cleanup(self) -> int:
        """Удалить старые оперативные записи, но никогда не трогать историю ETA."""
        if RETENTION_DAYS <= 0:
            return 0
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
