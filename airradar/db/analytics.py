"""Mixin Database: угрозы, сущности, статистика, дайджест."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from kyiv_time import kyiv_hour

logger = logging.getLogger(__name__)


class AnalyticsMixin:
    """угрозы, сущности, статистика, дайджест. Использует self._lock и self._conn из Database."""


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


    def active_threats(
        self, within_seconds: int = 1800, include_reported: bool = False
    ) -> list[dict[str, Any]]:
        """Свежие активные инциденты.

        include_reported=False (по умолчанию) — только подтверждённые 2+
        независимыми источниками: «Текущие угрозы» бота остаются строгими.
        include_reported=True — добавляет одиночные `reported`-события и
        стадию unknown (карта рендерит их полупрозрачно: «одне джерело —
        очікує підтвердження»), иначе карта пуста, пока источники не
        сойдутся. `disputed`/`retracted` не отдаются никогда.

        Стадия 'potential' («могут быть пуски», «загроза застосування»)
        на карту НЕ отдаётся: это домыслы, а не то, что летит. Карта
        показывает imminent (летит) и unknown (тип уточняется).
        """
        try:
            cutoff = int(time.time()) - within_seconds
            if include_reported:
                stage_sql = "i.stage IN ('imminent', 'unknown')"
                status_sql = "i.status IN ('reported', 'corroborated', 'officially_confirmed')"
            else:
                stage_sql = "i.stage IN ('imminent', 'potential')"
                status_sql = "i.status IN ('corroborated', 'officially_confirmed')"
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT i.updated_ts AS ts, i.weapon_class AS type, i.region, i.status, i.source_count, "
                    "i.origin_region AS origin, i.destination_region AS destination, "
                    "(SELECT text FROM incident_evidence e WHERE e.incident_key=i.incident_key ORDER BY e.id DESC LIMIT 1) AS text "
                    f"FROM incidents i WHERE i.updated_ts >= ? AND {stage_sql} "
                    f"AND {status_sql} ORDER BY i.updated_ts DESC",
                    (cutoff,),
                )
                return [dict(row) for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать активные угрозы: %s", exc)
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
                    h = kyiv_hour(row["ts"])  # час по Киеву (пост «хвилі ~X:00»)
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
