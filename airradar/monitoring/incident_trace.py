"""incident_trace.py — журнал обробки події (Incident Replay) і безпечний повтор.

Кожен етап конвеєра пише крок у event_trace. Replay проганяє збережений СИРИЙ текст
через ПОТОЧНІ парсер і safety_policy та повертає порівняння — нічого не відправляє й не пише в
чергу сповіщень (функція чиста щодо зовнішнього світу).
"""
from __future__ import annotations

import json
import time

STAGES = ("received", "source_resolved", "filter", "parse", "geo", "dedup", "trust", "safety",
          "published", "rejected", "notify_built", "notify_sent", "notify_error")
PARSER_VERSION = "threat_parser/1"
POLICY_VERSION = "safety_policy/1"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS event_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL,
        ts REAL NOT NULL,
        stage TEXT NOT NULL,
        source TEXT,
        ok INTEGER NOT NULL DEFAULT 1,
        data TEXT,
        parser_version TEXT,
        policy_version TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trace_event ON event_trace(event_key, ts)",
    "CREATE INDEX IF NOT EXISTS idx_trace_ts ON event_trace(ts)",
)


def ensure_schema(db) -> None:
    done = db.__dict__.setdefault("_ar_schema_done", set())  # DDL раз на екземпляр БД, а не на кожен виклик
    if "event_trace" in done:
        return
    with db._lock:  # noqa: SLF001
        for sql in _SCHEMA:
            db._conn.execute(sql)  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    done.add("event_trace")


def record(db, event_key: str, stage: str, *, source: str = "", ok: bool = True, data: dict | None = None,
           now: float | None = None) -> None:
    """Записати етап. Помилки журналу не повинні ламати конвеєр."""
    try:
        ensure_schema(db)
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)[:4000]
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                "INSERT INTO event_trace (event_key,ts,stage,source,ok,data,parser_version,policy_version) VALUES (?,?,?,?,?,?,?,?)",
                (event_key, (time.time() if now is None else now), stage, source, 1 if ok else 0, payload, PARSER_VERSION, POLICY_VERSION))
            db._conn.commit()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def timeline(db, event_key: str) -> list[dict]:
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT * FROM event_trace WHERE event_key=? ORDER BY ts, id", (event_key,)).fetchall()  # noqa: SLF001
    out, prev = [], None
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
        d["delta_ms"] = None if prev is None else int((d["ts"] - prev) * 1000)
        prev = d["ts"]
        out.append(d)
    return out


def search(db, *, source: str = "", text: str = "", since_ts: float = 0, limit: int = 50) -> list[dict]:
    """Пошук подій за джерелом / підрядком у даних (тип, місто) / часом."""
    ensure_schema(db)
    q = "SELECT event_key, MIN(ts) AS first_ts, MAX(ts) AS last_ts, MAX(source) AS source, MIN(ok) AS all_ok FROM event_trace WHERE ts >= ?"
    args: list = [since_ts]
    if source:
        q += " AND source = ?"; args.append(source)
    if text:
        q += " AND (data LIKE ? OR event_key = ?)"; args += [f"%{text}%", text]
    q += " GROUP BY event_key ORDER BY last_ts DESC LIMIT ?"; args.append(limit)
    with db._lock:  # noqa: SLF001
        return [dict(r) for r in db._conn.execute(q, args).fetchall()]  # noqa: SLF001


def replay(db, event_key: str) -> dict:
    """Безпечний replay: прогнати збережений текст через поточні парсер+політику. БЕЗ відправок."""
    tl = timeline(db, event_key)
    raw = next((s["data"].get("text") for s in tl if s["stage"] == "received" and s["data"].get("text")), None)
    if raw is None:
        return {"event_key": event_key, "replayed": False, "reason": "no_raw_text", "timeline": tl}
    import safety_policy
    import threat_parser
    result: dict = {"event_key": event_key, "replayed": True, "dry_run": True, "sent_notifications": 0,
                    "parser_version": PARSER_VERSION, "policy_version": POLICY_VERSION}
    try:
        parsed = threat_parser.parse_post(raw)
        result["parsed"] = repr(parsed)[:500]
    except Exception as exc:  # noqa: BLE001
        result["parse_error"] = type(exc).__name__
    verdict = safety_policy.check_publication(raw, llm_generated=False)
    result["policy_allowed"] = verdict.allowed
    result["policy_violations"] = verdict.violations
    result["original_outcome"] = "published" if any(s["stage"] == "published" for s in tl) else "rejected_or_unknown"
    return result


RETENTION_S = 30 * 86400


def prune(db, now: float | None = None) -> int:
    """Видалити стару трасу (за замовчуванням > 30 днів) — контроль росту БД."""
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM event_trace WHERE ts < ?", ((time.time() if now is None else now) - RETENTION_S,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        return cur.rowcount
