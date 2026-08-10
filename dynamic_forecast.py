"""Динамические ETA и вероятность исхода на нормализованной истории."""

from __future__ import annotations

import math
import sqlite3
import time
from typing import Any

from database import Database

HISTORY_DAYS = 3650
MATCH_WINDOW = 60 * 60
MIN_ETA_SAMPLES = 5
MIN_RISK_SAMPLES = 12
HORIZON_SECONDS = 30 * 60


def _quantile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = int(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _event_rows(db: Database, region: str, weapon: str, stage: str | None = None) -> list[sqlite3.Row]:
    conn = db._conn
    if conn is None:
        return []
    since = int(time.time()) - HISTORY_DAYS * 86400
    query = "SELECT * FROM threat_events WHERE region = ? AND weapon_class = ? AND event_ts >= ?"
    params: list[Any] = [region, weapon, since]
    if stage:
        query += " AND stage = ?"
        params.append(stage)
    return conn.execute(query + " ORDER BY event_ts", params).fetchall()


def estimate_dynamic_eta(db: Database, region: str, weapon: str) -> dict[str, Any]:
    """Сопоставить движение с ближайшим неиспользованным подтверждённым исходом."""
    try:
        launches = _event_rows(db, region, weapon, "movement") + _event_rows(db, region, weapon, "launch")
        launches.sort(key=lambda row: row["event_ts"])
        outcomes = _event_rows(db, region, weapon, "impact")
        used: set[int] = set()
        delays: list[float] = []
        for launch in launches:
            for outcome in outcomes:
                if outcome["id"] in used:
                    continue
                delta = outcome["event_ts"] - launch["event_ts"]
                if 0 < delta <= MATCH_WINDOW:
                    delays.append(float(delta))
                    used.add(outcome["id"])
                    break
        if len(delays) < MIN_ETA_SAMPLES:
            return {"available": False, "samples": len(delays)}
        return {
            "available": True,
            "samples": len(delays),
            "median_seconds": _quantile(delays, 0.5),
            "p20_seconds": _quantile(delays, 0.2),
            "p80_seconds": _quantile(delays, 0.8),
        }
    except sqlite3.Error:
        return {"available": False, "samples": 0}


def estimate_impact_risk(db: Database, region: str, weapon: str) -> dict[str, Any]:
    """P(impact <=30m | launch/movement) со сглаживанием Лапласа.

    Каждый исторический запуск — наблюдение; исход — наличие одного свободного
    impact такого же класса в регионе в горизонте. Это прогноз, не гарантия.
    """
    try:
        triggers = _event_rows(db, region, weapon, "movement") + _event_rows(db, region, weapon, "launch")
        triggers.sort(key=lambda row: row["event_ts"])
        outcomes = _event_rows(db, region, weapon, "impact")
        used: set[int] = set()
        successes = 0
        for trigger in triggers:
            for outcome in outcomes:
                if outcome["id"] in used:
                    continue
                delta = outcome["event_ts"] - trigger["event_ts"]
                if 0 < delta <= HORIZON_SECONDS:
                    successes += 1
                    used.add(outcome["id"])
                    break
        total = len(triggers)
        if total < MIN_RISK_SAMPLES:
            return {"available": False, "samples": total, "successes": successes}
        # Beta(1,1) prior: не выдаёт 0%/100% на конечной выборке.
        probability = (successes + 1) / (total + 2)
        return {"available": True, "samples": total, "successes": successes,
                "probability": probability, "horizon_minutes": HORIZON_SECONDS // 60}
    except sqlite3.Error:
        return {"available": False, "samples": 0, "successes": 0}
