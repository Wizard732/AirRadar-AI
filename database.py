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
from typing import Any

from airradar.db.admin_channels import AdminChannelsMixin
from airradar.db.alerts import AlertsMixin
from airradar.db.analytics import AnalyticsMixin
from airradar.db.feedback import FeedbackMixin
from airradar.db.guard import GuardMixin
from airradar.db.incidents import IncidentsMixin
from airradar.db.users import UsersMixin

logger = logging.getLogger(__name__)

# Исторические события нужны для ETA/вероятностей; не удаляем их по времени.
# Операционная очистка касается только дедупликации, а не фактов угроз.
RETENTION_DAYS = 0


class Database(
    AdminChannelsMixin,
    GuardMixin,
    IncidentsMixin,
    AlertsMixin,
    UsersMixin,
    FeedbackMixin,
    AnalyticsMixin,
):
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
        # WAL + busy timeout keep the live bot and the five-minute recovery
        # sync from failing each other with "database is locked".
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
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
            # Проактивные уведомления «можлива нова тривога» (статистика волн):
            # одно на эпизод отбоя. UNIQUE защищает от повторной рассылки,
            # если бот перезапустился в пределах окна уведомления.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS wave_notices (
                    region          TEXT    NOT NULL,
                    episode_end_ts  INTEGER NOT NULL,
                    sent_ts         INTEGER NOT NULL,
                    UNIQUE(region, episode_end_ts)
                )
                """
            )
            # Проактивные уведомления «відбій орієнтовно за ~N хв»: одно на
            # эпизод полёта (ключ — ts последнего полётного поста). Новый
            # полётный пост = новый эпизод = можно уведомить снова.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS standdown_notices (
                    region    TEXT    NOT NULL,
                    flight_ts INTEGER NOT NULL,
                    sent_ts   INTEGER NOT NULL,
                    UNIQUE(region, flight_ts)
                )
                """
            )
            # Привязка countdown к эпизоду тревоги: один countdown на эпизод,
            # повтор только если оценка изменилась на ≥10 мин. Мягкая миграция.
            try:
                cur.execute(
                    "ALTER TABLE standdown_notices ADD COLUMN alert_started_ts INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            try:
                cur.execute(
                    "ALTER TABLE standdown_notices ADD COLUMN remaining_min INTEGER"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_end ON alerts(region, ended_ts)")
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
            # Метрика «випередження сирени»: наш первый пост по эпизоду тревоги
            # против alert_start того же эпизода. UNIQUE защищает от повторов
            # (одна тревога — один замер).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS siren_lead (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    region         TEXT NOT NULL,
                    first_post_ts  INTEGER NOT NULL,
                    alert_ts       INTEGER NOT NULL,
                    seconds        INTEGER NOT NULL,
                    UNIQUE(region, alert_ts)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_siren_lead_ts ON siren_lead(alert_ts)")
            # Фидбек подписчиков под алертами: счётчики на пост (ключ — хеш текста).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_feedback (
                    message_key  TEXT PRIMARY KEY,
                    ts           INTEGER NOT NULL,
                    useful       INTEGER NOT NULL DEFAULT 0,
                    noise        INTEGER NOT NULL DEFAULT 0,
                    error        INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Мягкая миграция старых БД: кнопка «❌ Помилка» добавила третий голос.
            try:
                cur.execute(
                    "ALTER TABLE alert_feedback ADD COLUMN error INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            # Ночной режим: персональная настройка (по умолчанию выкл).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_prefs (
                    user_id     INTEGER PRIMARY KEY,
                    night_mode  INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Персональный фильтр типов угроз: CSV разрешённых групп
            # ('ballistic'/'uav'/'other'; '' = все классы). Мягкая миграция.
            try:
                cur.execute(
                    "ALTER TABLE user_prefs ADD COLUMN weapon_classes TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            # Режим «Укриття» (персональный): доставляем только тревогу
            # (критичные классы) и відбій — без апдейтов и нотисов.
            try:
                cur.execute(
                    "ALTER TABLE user_prefs ADD COLUMN shelter_mode INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            # «Моя зона»: slug города/района/области, выбранного по геопозиции.
            # Пусто = зона не задана (маркер «ваша зона» в постах не ставится).
            try:
                cur.execute(
                    "ALTER TABLE user_prefs ADD COLUMN home_region TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # колонка уже существует
            # Задержка конвейера «пост источника → пост в канале» (мс).
            # Основа метрики p50/p95 в админке /status.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_latency (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,
                    latency_ms  INTEGER NOT NULL
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_latency_ts ON pipeline_latency(ts)")
            # Официальные сирены (alerts.in.ua): последнее известное состояние
            # «тривога/нет» по регионам. Пишется только при заданном токене.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS siren_states (
                    region      TEXT PRIMARY KEY,
                    air_raid    INTEGER NOT NULL DEFAULT 0,
                    updated_ts  INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Журнал офіційних тривог alerts.in.ua: для вкладок «Історія», «Тривалості», «Статистика» на карті.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_log (
                    id          INTEGER PRIMARY KEY,
                    slug        TEXT NOT NULL,
                    loc_type    TEXT NOT NULL DEFAULT '',
                    title       TEXT NOT NULL DEFAULT '',
                    raion       TEXT NOT NULL DEFAULT '',
                    level       TEXT NOT NULL DEFAULT 'red',
                    kind        TEXT NOT NULL DEFAULT '',
                    started_ts  INTEGER NOT NULL,
                    ended_ts    INTEGER NOT NULL DEFAULT 0,
                    last_seen   INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_log_started ON alert_log(started_ts)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      INTEGER NOT NULL,
                    slug    TEXT NOT NULL DEFAULT '',
                    title   TEXT NOT NULL DEFAULT '',
                    raion   TEXT NOT NULL DEFAULT '',
                    kind    TEXT NOT NULL,
                    detail  TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events(ts)")
            # Репорты угроз от пользователей (share location в боте).
            # Координаты храним округлёнными до ~100 м; в публичный API не
            # отдаём user_id — только точку и возраст.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS geo_reports (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id  INTEGER NOT NULL,
                    ts       INTEGER NOT NULL,
                    lat      REAL    NOT NULL,
                    lon      REAL    NOT NULL,
                    region   TEXT    NOT NULL DEFAULT '',
                    text     TEXT    NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_reports_ts ON geo_reports(ts)")
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
            # Структурные наблюдения из threat_parser: одна строка = один объект/
            # группа в конкретном месте. Из них карта строит треки (вектор,
            # скорость), поэтому храним координаты и время самого наблюдения.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS track_observations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_ts    INTEGER NOT NULL,        -- время сообщения
                    as_of       TEXT    NOT NULL DEFAULT '',  -- «станом на HH:MM»
                    source      TEXT    NOT NULL DEFAULT '',
                    weapon      TEXT    NOT NULL,
                    profile     TEXT    NOT NULL DEFAULT '',
                    count       INTEGER,
                    group_count INTEGER,
                    oblast      TEXT    NOT NULL DEFAULT '',
                    place       TEXT    NOT NULL DEFAULT '',
                    relation    TEXT    NOT NULL DEFAULT 'at',
                    lat         REAL,
                    lon         REAL,
                    precision   TEXT    NOT NULL DEFAULT 'none',
                    heading_from TEXT   NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_obs_ts ON track_observations(event_ts)")
            # «Гео-сторож»: точка пользователя (live-локация или фиксированная) для
            # персональных предупреждений о приближении цели. Точные координаты
            # хранятся только пока сторож включён и удаляются при выключении.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS guard_points (
                    user_id    INTEGER PRIMARY KEY,
                    enabled    INTEGER NOT NULL DEFAULT 0,
                    radius_km  REAL    NOT NULL DEFAULT 10,
                    lat        REAL,
                    lon        REAL,
                    live       INTEGER NOT NULL DEFAULT 0,
                    updated_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS district_warn_state (
                    track_key TEXT NOT NULL,
                    region    TEXT NOT NULL,
                    sent_ts   INTEGER NOT NULL,
                    PRIMARY KEY (track_key, region)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS guard_state (
                    user_id   INTEGER NOT NULL,
                    track_key TEXT    NOT NULL,
                    level     TEXT    NOT NULL,
                    distance  REAL    NOT NULL DEFAULT 0,
                    sent_ts   INTEGER NOT NULL,
                    PRIMARY KEY (user_id, track_key)
                )
                """
            )
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
            # Звіти користувачів про помилки (категорія) — черга модерації та датасет для парсера.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS error_reports (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    message_key TEXT    NOT NULL,
                    category    TEXT    NOT NULL,
                    status      TEXT    NOT NULL DEFAULT 'open'
                )
                """
            )
            # Журнал доставки сповіщень: created→queued→sent→delivered→opened→acknowledged | failed.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_delivery (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key   TEXT    NOT NULL,
                    channel     TEXT    NOT NULL,
                    importance  TEXT    NOT NULL,
                    status      TEXT    NOT NULL,
                    created_ts  INTEGER NOT NULL,
                    updated_ts  INTEGER NOT NULL,
                    UNIQUE (event_key, channel)
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

    def add_observations(self, event_ts: int, source: str, observations, as_of: str = "") -> int:
        """Записать наблюдения threat_parser; возвращает число вставленных строк."""
        rows = [
            (event_ts, as_of, source, o.weapon, o.profile, o.count, o.group_count, o.oblast, o.place,
             o.relation, o.lat, o.lon, o.precision, o.heading_from)
            for o in observations
        ]
        if not rows:
            return 0
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.executemany(
                    "INSERT INTO track_observations (event_ts, as_of, source, weapon, profile, count, group_count, "
                    "oblast, place, relation, lat, lon, precision, heading_from) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._conn.commit()
            return len(rows)
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать наблюдения: %s", exc)
            return 0

    def recent_observations(self, since_ts: int, weapon: str = "") -> list[dict[str, Any]]:
        """Наблюдения с since_ts (старые → новые) для построения треков карты."""
        sql = "SELECT * FROM track_observations WHERE event_ts >= ?"
        args: list[Any] = [since_ts]
        if weapon:
            sql += " AND weapon = ?"
            args.append(weapon)
        sql += " ORDER BY event_ts, id"
        with self._lock:
            assert self._conn is not None
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

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

    def forget_fingerprint(self, fingerprint: str) -> None:
        """Прибрати відбиток із persistent-дедуплікації (після збою обробки поста)."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("DELETE FROM seen_hashes WHERE fp = ?", (fingerprint,))
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Дедупликация (БД): не удалось забыть отпечаток: %s", exc)

    def set_dedup_window(self, seconds: int) -> None:
        """Установить окно дедупликации (сек). По умолчанию 1 час."""
        self._dedup_window = seconds

    ERROR_CATEGORIES = ("wrong_city", "wrong_type", "stale", "duplicate", "unclear")

    # ------------------------------------------------------------------
    #  Персональный фильтр типов угроз (weapon_classes)
    # ------------------------------------------------------------------
    # Разрешённые группы фильтра. '' или «все группы» = без фильтра.
    # Канонические классы маппятся в группы в weapon_group().
    WEAPON_GROUPS = ("ballistic", "uav", "other")

    # ------------------------------------------------------------------
    #  Репорты угроз от пользователей (share location)
    # ------------------------------------------------------------------
    GEO_REPORT_COOLDOWN_S = 300  # не чаще одного репорта в 5 минут от юзера

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
