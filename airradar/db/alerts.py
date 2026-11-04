"""Mixin Database: тревоги, сирены, уведомления об отбое."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from kyiv_time import fmt as kyiv_fmt, kyiv_midnight_ts

logger = logging.getLogger(__name__)


class AlertsMixin:
    """тревоги, сирены, уведомления об отбое. Использует self._lock и self._conn из Database."""


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


    def recently_ended_alerts(self, within_seconds: int) -> list[dict[str, Any]]:
        """Регионы с завершённой тревогой за последние N секунд.

        Возвращает [{region, ended_ts}] — последний эпизод каждого региона.
        Основа фонового цикла «можлива нова тривога»: после отбоя статистика
        волн подсказывает, когда ждать следующую волну.
        """
        try:
            cutoff = int(time.time()) - int(within_seconds)
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT region, MAX(ended_ts) AS ended_ts FROM alerts "
                    "WHERE ended_ts IS NOT NULL AND ended_ts >= ? "
                    "GROUP BY region",
                    (cutoff,),
                ).fetchall()
                return [{"region": r["region"], "ended_ts": int(r["ended_ts"])} for r in rows]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать завершённые тревоги: %s", exc)
            return []


    def alert_counts_by_day(self, region: str, days: int = 7, *, now: int | None = None) -> list[tuple[str, int]]:
        """Число эпизодов тревог по дням (по started_ts, киевское время).

        Возвращает [(«дд.мм», count)] для графика «📈 Тиждень»: days точек,
        старшая — первой. Эпизоды считаются по дню старта тревоги.
        """
        now = int(now if now is not None else time.time())
        day_start = kyiv_midnight_ts(now)  # полночь по Киеву, не по UTC
        out: list[tuple[str, int]] = []
        try:
            with self._lock:
                assert self._conn is not None
                for offset in range(days - 1, -1, -1):
                    start = day_start - offset * 86400
                    end = start + 86400
                    row = self._conn.execute(
                        "SELECT COUNT(*) AS c FROM alerts "
                        "WHERE region = ? AND started_ts >= ? AND started_ts < ?",
                        (region, start, end),
                    ).fetchone()
                    label = kyiv_fmt(start, "%d.%m")
                    out.append((label, int(row["c"]) if row else 0))
        except sqlite3.Error as exc:
            logger.warning("Не удалось собрать статистику тревог по дням: %s", exc)
            return []
        return out


    def record_wave_notice(self, region: str, episode_end_ts: int, *, sent_ts: int | None = None) -> None:
        """Запомнить, что уведомление «можлива тривога» по этому эпизоду отправлено."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO wave_notices (region, episode_end_ts, sent_ts) VALUES (?, ?, ?)",
                    (region, int(episode_end_ts), int(sent_ts if sent_ts is not None else time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать wave-notice: %s", exc)


    def wave_notice_sent(self, region: str, episode_end_ts: int) -> bool:
        """True, если уведомление по этому эпизоду уже отправлялось."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM wave_notices WHERE region = ? AND episode_end_ts = ?",
                    (region, int(episode_end_ts)),
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False


    def record_standdown_notice(
        self,
        region: str,
        flight_ts: int,
        *,
        sent_ts: int | None = None,
        alert_started_ts: int = 0,
        remaining_min: int | None = None,
    ) -> None:
        """Запомнить, что «відбій орієнтовно за ~N хв» по этому полёту отправлено.

        alert_started_ts — эпизод тревоги, remaining_min — оценка на момент
        отправки (для правила «повтор только при изменении оценки ≥10 мин»).
        """
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT OR IGNORE INTO standdown_notices "
                    "(region, flight_ts, sent_ts, alert_started_ts, remaining_min) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        region,
                        int(flight_ts),
                        int(sent_ts if sent_ts is not None else time.time()),
                        int(alert_started_ts),
                        int(remaining_min) if remaining_min is not None else None,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать standdown-notice: %s", exc)


    def standdown_notice_sent(self, region: str, flight_ts: int) -> bool:
        """True, если countdown отбоя по этому полётному посту уже отправляли."""
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM standdown_notices WHERE region = ? AND flight_ts = ?",
                    (region, int(flight_ts)),
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False


    def standdown_notice_sent_recent(self, region: str, cooldown_sec: int) -> bool:
        """True, если по региону countdown отправляли в последние cooldown_sec.

        Дедуп-кулдаун: посты «цель в воздухе» прилетают каждые 1–3 минуты,
        и старый дедуп по точному flight_ts считал каждый новый пост новым
        эпизодом → countdown спамился 5–10 раз за тревогу. Кулдаун на регион
        делает «одно уведомление на период», независимо от числа полётных
        постов.
        """
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT 1 FROM standdown_notices "
                    "WHERE region = ? AND sent_ts >= ? LIMIT 1",
                    (region, int(time.time()) - int(cooldown_sec)),
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False


    def standdown_episode_notice(self, region: str, alert_started_ts: int) -> dict | None:
        """Последний countdown этого эпизода тревоги.

        Возвращает {"remaining_min": int, "sent_ts": int} или None, если на
        этом эпизоде (alerts.started_ts) ещё не отправляли. remaining_min —
        оценка «за сколько хв» на момент отправки: правило повтора —
        «оценка изменилась на ≥10 минут».
        """
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT remaining_min, sent_ts FROM standdown_notices "
                    "WHERE region = ? AND alert_started_ts = ? "
                    "AND remaining_min IS NOT NULL "
                    "ORDER BY sent_ts DESC LIMIT 1",
                    (region, int(alert_started_ts)),
                ).fetchone()
                if row is None:
                    return None
                return {"remaining_min": int(row["remaining_min"]), "sent_ts": int(row["sent_ts"])}
        except sqlite3.Error:
            return None


    def expire_stale_alerts(self, max_age_seconds: int = 6 * 3600) -> int:
        """Close only stale local alert records; no claim of an official all-clear."""
        try:
            now = int(time.time())
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "UPDATE alerts SET ended_ts=? WHERE ended_ts IS NULL AND started_ts<?",
                    (now, now - max_age_seconds),
                )
                self._conn.commit()
                return cur.rowcount
        except sqlite3.Error as exc:
            logger.warning("Не удалось закрыть устаревшие тревоги: %s", exc)
            return 0


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
    #  Метрика «випередження сирени» + фидбек + ночной режим
    # ------------------------------------------------------------------
    def record_siren_lead(self, region: str, first_post_ts: int, alert_ts: int) -> bool:
        """Сохранить замер: наш пост против старта официальной тревоги.

        seconds > 0 — мы раньше сирены. UNIQUE(region, alert_ts): одна тревога
        — один замер, повторы игнорируются.
        """
        try:
            with self._lock:
                assert self._conn is not None
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO siren_lead (region, first_post_ts, alert_ts, seconds) "
                    "VALUES (?, ?, ?, ?)",
                    (region, first_post_ts, alert_ts, alert_ts - first_post_ts),
                )
                self._conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать замер сирены: %s", exc)
            return False


    def siren_lead_stats(self, days: int = 7) -> dict:
        """Сводка опережения сирены за период: {episodes, avg_lead_sec, before_count}.

        before_count — сколько эпизодов мы поймали раньше официальной тревоги
        (first_post_ts < alert_ts).
        """
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT COUNT(*) AS episodes, "
                    "AVG(CASE WHEN seconds > 0 THEN seconds END) AS avg_lead, "
                    "SUM(CASE WHEN seconds > 0 THEN 1 ELSE 0 END) AS before_count "
                    "FROM siren_lead WHERE alert_ts >= ?",
                    (int(time.time()) - days * 86400,),
                ).fetchone()
                return {
                    "episodes": int(row["episodes"] or 0),
                    "avg_lead_sec": float(row["avg_lead"] or 0),
                    "before_count": int(row["before_count"] or 0),
                }
        except sqlite3.Error as exc:
            logger.warning("Не удалось посчитать статистику сирены: %s", exc)
            return {"episodes": 0, "avg_lead_sec": 0.0, "before_count": 0}


    # ------------------------------------------------------------------
    #  Официальные сирены (alerts.in.ua, опционально — по токену)
    # ------------------------------------------------------------------
    def set_siren_state(self, region: str, active: bool) -> None:
        """Сохранить последнее известное состояние сирены региона."""
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO siren_states (region, air_raid, updated_ts) VALUES (?, ?, ?) "
                    "ON CONFLICT(region) DO UPDATE SET air_raid = ?, updated_ts = ?",
                    (region, 1 if active else 0, int(time.time()),
                     1 if active else 0, int(time.time())),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Не удалось сохранить состояние сирен: %s", exc)


    def alert_sync(self, entries: list[dict[str, Any]], now: int) -> None:
        """Синхронізувати журнал із поточним списком активних тривог (id, slug, type, title, raion, level, kind, started).

        Нові → подія «start», зник → «end» (відбій), змінився рівень → «level». Перший запуск (порожній журнал)
        подій не пише, щоб не засипати «Джерела» початковим станом."""
        try:
            with self._lock:
                assert self._conn is not None
                c = self._conn
                primed = c.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] > 0
                open_rows = {r["id"]: r for r in c.execute("SELECT * FROM alert_log WHERE ended_ts = 0")}
                seen = set()
                for e in entries:
                    aid = int(e.get("id") or 0)
                    if not aid:
                        continue
                    seen.add(aid)
                    row = open_rows.get(aid)
                    started = int(e.get("started_ts") or now)
                    if row is None:
                        c.execute(
                            "INSERT OR REPLACE INTO alert_log (id, slug, loc_type, title, raion, level, kind, started_ts, ended_ts, last_seen) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                            (aid, e["slug"], e["type"], e["title"], e.get("raion", ""), e["level"], e["kind"], started, now),
                        )
                        if primed:
                            c.execute("INSERT INTO alert_events (ts, slug, title, raion, kind, detail) VALUES (?,?,?,?,?,?)",
                                      (now, e["slug"], e["title"], e.get("raion", ""), "start", e["level"]))
                    else:
                        if row["level"] != e["level"]:
                            c.execute("INSERT INTO alert_events (ts, slug, title, raion, kind, detail) VALUES (?,?,?,?,?,?)",
                                      (now, e["slug"], e["title"], e.get("raion", ""), "level", f"{row['level']}>{e['level']}"))
                        c.execute("UPDATE alert_log SET level = ?, last_seen = ? WHERE id = ?", (e["level"], now, aid))
                for aid, row in open_rows.items():
                    if aid not in seen:
                        c.execute("UPDATE alert_log SET ended_ts = ? WHERE id = ?", (now, aid))
                        c.execute("INSERT INTO alert_events (ts, slug, title, raion, kind, detail) VALUES (?,?,?,?,?,?)",
                                  (now, row["slug"], row["title"], row["raion"], "end", row["level"]))
                c.execute("DELETE FROM alert_log WHERE started_ts < ? AND ended_ts > 0", (now - 40 * 86400,))
                c.execute("DELETE FROM alert_events WHERE ts < ?", (now - 40 * 86400,))
                c.commit()
        except sqlite3.Error as exc:
            logger.warning("alert_sync: %s", exc)


    def alert_history(self, since_ts: int) -> dict[str, list]:
        """Тривоги, що перетинають [since_ts, now], і події за той самий час (для вкладок історії/статистики)."""
        try:
            with self._lock:
                assert self._conn is not None
                al = self._conn.execute(
                    "SELECT id, slug, loc_type, title, raion, level, kind, started_ts, ended_ts FROM alert_log "
                    "WHERE ended_ts = 0 OR ended_ts >= ? ORDER BY started_ts", (since_ts,)).fetchall()
                ev = self._conn.execute(
                    "SELECT ts, slug, title, raion, kind, detail FROM alert_events WHERE ts >= ? ORDER BY ts DESC LIMIT 600",
                    (since_ts,)).fetchall()
            return {"alerts": [list(r) for r in al], "events": [list(r) for r in ev]}
        except sqlite3.Error as exc:
            logger.warning("alert_history: %s", exc)
            return {"alerts": [], "events": []}


    def siren_states_active(self) -> list[str]:
        """Регионы с активной сиреной по последнему опросу (пусто = нет данных)."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT region FROM siren_states WHERE air_raid = 1"
                ).fetchall()
                return [r["region"] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать сирены: %s", exc)
            return []


    def siren_states_fresh(self, max_age_sec: int = 900) -> list[str]:
        """Активные сирены, если данные обновлены недавно (иначе полл мёртв)."""
        try:
            with self._lock:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT region FROM siren_states "
                    "WHERE air_raid = 1 AND updated_ts >= ?",
                    (int(time.time()) - int(max_age_sec),),
                ).fetchall()
                return [r["region"] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("Не удалось прочитать сирены (fresh): %s", exc)
            return []


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
