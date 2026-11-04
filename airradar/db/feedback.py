"""Mixin Database: обратная связь, доставка, задержки, гео-репорты."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


class FeedbackMixin:
    """обратная связь, доставка, задержки, гео-репорты. Использует self._lock и self._conn из Database."""


    def add_alert_feedback(self, message_key: str, vote: str) -> None:
        """Учесть голос по посту: vote 'useful' | 'noise' | 'error' (голоса
        независимы — счётчики; повторный клик той же кнопки увеличивает счёт)."""
        if vote not in ("useful", "noise", "error"):
            return
        column = vote
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    f"INSERT INTO alert_feedback (message_key, ts, {column}) VALUES (?, ?, 1) "
                    f"ON CONFLICT(message_key) DO UPDATE SET {column} = {column} + 1",
                    (message_key, int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось учесть фидбек: %s", exc)


    def add_error_report(self, user_id: int, message_key: str, category: str) -> bool:
        """Записати звіт про помилку; невідома категорія відхиляється."""
        if category not in self.ERROR_CATEGORIES:
            return False
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO error_reports (ts, user_id, message_key, category) VALUES (?, ?, ?, ?)",
                    (int(time.time()), int(user_id), message_key, category),
                )
                self._conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать отчёт об ошибке: %s", exc)
            return False


    def error_report_counts(self, status: str = "open") -> dict:
        """Кількість звітів за категоріями (для панелі модерації)."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT category, COUNT(*) c FROM error_reports WHERE status = ? GROUP BY category", (status,)
                ).fetchall()
            return {r["category"]: r["c"] for r in rows}
        except sqlite3.Error:
            return {}


    def record_delivery(self, event_key: str, channel: str, importance: str, status: str) -> bool:
        """Оновити статус доставки лише вперед (див. notification_rules.can_transition)."""
        from notification_rules import Delivery, can_transition
        now = int(time.time())
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT status FROM notification_delivery WHERE event_key = ? AND channel = ?",
                    (event_key, channel),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO notification_delivery (event_key, channel, importance, status, created_ts, updated_ts)"
                        " VALUES (?, ?, ?, ?, ?, ?)", (event_key, channel, importance, status, now, now))
                elif can_transition(Delivery(row["status"]), Delivery(status)):
                    self._conn.execute(
                        "UPDATE notification_delivery SET status = ?, updated_ts = ? WHERE event_key = ? AND channel = ?",
                        (status, now, event_key, channel))
                else:
                    return False
                self._conn.commit()
            return True
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("Не удалось записать статус доставки: %s", exc)
            return False


    def delivery_summary(self, since_ts: int = 0) -> dict:
        """Скільки сповіщень у кожному статусі (для моніторингу)."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) c FROM notification_delivery WHERE created_ts >= ? GROUP BY status",
                    (since_ts,)).fetchall()
            return {r["status"]: r["c"] for r in rows}
        except sqlite3.Error:
            return {}


    def audit_summary(self, since_ts: int) -> dict:
        """Рішення LLM-аудиту за період: {decision: n} (для ІІ-аналізатора)."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT decision, COUNT(*) c FROM inference_audit WHERE created_ts >= ? GROUP BY decision",
                    (since_ts,)).fetchall()
            return {r["decision"]: r["c"] for r in rows}
        except sqlite3.Error:
            return {}


    def get_alert_feedback(self, message_key: str) -> dict:
        """Счётчики фидбека поста {useful, noise, error}."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT useful, noise, error FROM alert_feedback WHERE message_key = ?",
                    (message_key,),
                ).fetchone()
                return (
                    {"useful": row["useful"], "noise": row["noise"], "error": row["error"]}
                    if row else {"useful": 0, "noise": 0, "error": 0}
                )
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать фидбек: %s", exc)
            return {"useful": 0, "noise": 0, "error": 0}


    # ------------------------------------------------------------------
    #  Задержка конвейера «пост источника → пост в канале» (p50/p95)
    # ------------------------------------------------------------------
    def record_pipeline_latency(self, latency_ms: int) -> None:
        """Записать замер задержки публикации (мс) для метрики /status."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO pipeline_latency (ts, latency_ms) VALUES (?, ?)",
                    (int(time.time()), max(0, int(latency_ms))),
                )
                # Метрика — окно 48ч: старше не нужно, подчищаем чтобы не росло.
                self._conn.execute(
                    "DELETE FROM pipeline_latency WHERE ts < ?",
                    (int(time.time()) - 2 * 86400,),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать pipeline latency: %s", exc)


    def latency_percentiles(self, seconds: int = 86400) -> dict[str, Any]:
        """p50/p95 задержки конвейера за окно (мс) + число замеров."""
        cutoff = int(time.time()) - int(seconds)
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT latency_ms FROM pipeline_latency WHERE ts >= ? ORDER BY latency_ms",
                    (cutoff,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать pipeline latency: %s", exc)
            rows = []
        samples = [int(r["latency_ms"]) for r in rows]
        if not samples:
            return {"p50_ms": 0, "p95_ms": 0, "samples": 0}

        def _pct(p: float) -> int:
            idx = min(len(samples) - 1, max(0, round(p * (len(samples) - 1))))
            return samples[idx]

        return {"p50_ms": _pct(0.50), "p95_ms": _pct(0.95), "samples": len(samples)}


    def add_geo_report(
        self, user_id: int, lat: float, lon: float, text: str = "", region: str = ""
    ) -> bool:
        """Сохранить репорт с геопозицией юзера. False — сработал антиспам.

        Координаты округляются до 3 знаков (~100 м): публично показываем
        область, а не точку, где стоит человек.
        """
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT MAX(ts) AS last_ts FROM geo_reports WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                last_ts = int(row["last_ts"] or 0)
                now = int(time.time())
                if now - last_ts < self.GEO_REPORT_COOLDOWN_S:
                    return False
                self._conn.execute(
                    "INSERT INTO geo_reports (user_id, ts, lat, lon, region, text) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id, now,
                        round(float(lat), 3), round(float(lon), 3),
                        region, (text or "").strip()[:120],
                    ),
                )
                self._conn.commit()
                return True
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить geo-репорт: %s", exc)
            return False


    def seconds_until_geo_report_allowed(self, user_id: int) -> int:
        """Сколько секунд до следующего разрешённого репорта юзера."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT MAX(ts) AS last_ts FROM geo_reports WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                last_ts = int(row["last_ts"] or 0)
                remaining = self.GEO_REPORT_COOLDOWN_S - (int(time.time()) - last_ts)
                return max(0, remaining)
        except sqlite3.Error:
            return 0


    def recent_geo_reports(self, minutes: int = 90) -> list[dict[str, Any]]:
        """Свежие репорты для карты (без user_id): точка, возраст, текст."""
        try:
            cutoff = int(time.time()) - minutes * 60
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "SELECT ts, lat, lon, region, text FROM geo_reports "
                    "WHERE ts >= ? ORDER BY ts DESC",
                    (cutoff,),
                )
                now = int(time.time())
                return [
                    {
                        "ts": row["ts"],
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "region": row["region"],
                        "text": row["text"],
                        "age_min": max(0, (now - row["ts"]) // 60),
                    }
                    for row in cur.fetchall()
                ]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать geo-репорты: %s", exc)
            return []
