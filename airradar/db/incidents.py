"""Mixin Database: инциденты и их проверка."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


class IncidentsMixin:
    """инциденты и их проверка. Использует self._lock и self._conn из Database."""


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
        official: bool = False,
    ) -> dict[str, Any]:
        """Attach an explicit follow-up to a recent compatible open incident.

        official=True (пост из официального канала из OFFICIAL_SOURCES) —
        инцидент помечается 'officially_confirmed' сразу, как и в
        register_incident: публичная точность честно разделяет
        «підтверджено офіційним джерелом» и обычную корроборацию.
        """
        cutoff = event_ts - max(60, window_seconds)
        try:
            with self._lock:
                assert self._conn is not None
                # Стадія не ділить інцидент: «Балістика на Київ» (stage unknown) і «Загроза балістики для Києва» (imminent)
                # від двох каналів — одна подія. Сумісні: однакова стадія або одна з них 'unknown'.
                rows = [r for r in self._conn.execute(
                    "SELECT * FROM incidents WHERE state='open' AND updated_ts>=? "
                    "AND destination_region=? ORDER BY updated_ts DESC",
                    (cutoff, fact.destination_region),
                ).fetchall() if r["stage"] == fact.stage or "unknown" in (r["stage"], fact.stage)]
                row = next((candidate for candidate in rows if candidate["weapon_class"] == fact.weapon_class), None)
                if row is None and fact.is_delta and fact.weapon_class == "unknown" and len(rows) == 1:
                    row = rows[0]
                if row is None:
                    key = f"{event_ts}:{fact.destination_region}:{fact.weapon_class}:{fact.stage}"
                    groups = [source_group] if source_group else []
                    status = "officially_confirmed" if official else "reported"
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
                    if official or row["status"] == "officially_confirmed":
                        # Официальный пост подтверждает инцидент сразу и
                        # навсегда; подтверждённый статус не откатывается.
                        status = "officially_confirmed"
                    else:
                        status = (
                            "corroborated" if len(groups) >= confirmation_sources
                            else row["status"]
                        )
                    count_kind, count_value = row["count_kind"], row["count_value"]
                    if fact.count_kind == "delta" and fact.count_value is not None:
                        count_value = (count_value or 0) + fact.count_value
                        count_kind = "reported_total"
                    elif fact.count_kind == "exact" and count_value is None:
                        count_kind, count_value = "exact", fact.count_value
                    elif fact.count_kind == "exact" and count_value != fact.count_value:
                        count_kind = "conflicting"
                    stage_now = row["stage"] if fact.stage == "unknown" else fact.stage   # конкретна стадія витісняє unknown
                    self._conn.execute(
                        "UPDATE incidents SET updated_ts=?, source_count=?, sources=?, status=?, origin_region=?, count_kind=?, count_value=?, weapon_raw=?, stage=? WHERE incident_key=?",
                        (event_ts, len(groups), ",".join(groups), status, fact.origin_region or row["origin_region"],
                         count_kind, count_value, fact.raw_designation or row["weapon_raw"], stage_now, key),
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


    def confirmed_incident_counts(self, region: str | None = None, since: int = 0) -> dict[str, int]:
        """Count unique corroborated/official incidents, never raw channel posts."""
        try:
            with self._lock:
                assert self._conn is not None
                query = (
                    "SELECT weapon_class, COUNT(*) c FROM incidents WHERE updated_ts>=? "
                    "AND status IN ('corroborated', 'officially_confirmed') "
                    "AND state='open' AND stage IN ('imminent', 'potential')"
                )
                params: list[Any] = [since]
                if region:
                    query += " AND region=?"
                    params.append(region)
                query += " GROUP BY weapon_class"
                rows = self._conn.execute(query, params).fetchall()
                return {row["weapon_class"]: row["c"] for row in rows}
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать подтверждённые инциденты: %s", exc)
            return {}


    def confirmed_consequences(self, region: str, limit: int = 5, since: int = 0) -> list[dict[str, Any]]:
        """Return only corroborated/official impact reports with raw evidence."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT i.updated_ts AS ts, i.weapon_class AS type, i.source_count, i.status, "
                    "(SELECT text FROM incident_evidence e WHERE e.incident_key=i.incident_key ORDER BY e.id DESC LIMIT 1) AS text "
                    "FROM incidents i WHERE i.region=? AND i.updated_ts>=? AND i.stage='past' "
                    "AND i.status IN ('corroborated', 'officially_confirmed') AND i.state='open' "
                    "ORDER BY i.updated_ts DESC LIMIT ?",
                    (region, since, limit),
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать подтверждённые последствия: %s", exc)
            return []
