"""stats_engine.py — статистика угроз для бота и Mini App.

Источник — threat_events. Один и тот же вылет приходит из нескольких каналов и
постами-обновлениями, поэтому считаем «сигналы»: уникальные (вид, область,
30-минутное окно). Это честнее сырого числа постов, но всё равно не число
пусков: источники публикуют сводки, а не реестр.
"""
from __future__ import annotations

import time
from typing import Any

from kyiv_time import fmt as kyiv_fmt, kyiv_date, kyiv_hour, kyiv_midnight_ts
from regions import region_name

CATS: dict[str, tuple[str, str, frozenset[str]]] = {
    "drone": ("БпЛА", "🛸", frozenset({"shahed", "uav", "recon_drone"})),
    "missile": ("Ракети", "🚀", frozenset({"cruise_missile", "ballistic", "missile", "air_missile", "coastal_missile"})),
    "kab": ("КАБ", "💣", frozenset({"kab"})),
    "air": ("Авіація", "✈️", frozenset({"aviation", "tac_aviation", "strat_aviation"})),
    "fpv": ("FPV", "✦", frozenset({"fpv"})),
    "arty": ("РСЗО / арт.", "💥", frozenset({"mlrs", "artillery"})),
}
STAGES = ("launch", "movement", "intercept", "impact")
BUCKET_S = 1800
_SKIP_REGIONS = {"", "unknown", "multi"}


def category(weapon: str) -> str | None:
    for key, (_, _, ws) in CATS.items():
        if weapon in ws:
            return key
    return None


def _signals(db, since: int) -> set[tuple[str, str, int]]:
    out: set[tuple[str, str, int]] = set()
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            f"SELECT event_ts, weapon_class, region FROM threat_events WHERE event_ts>=? "
            f"AND stage IN ({','.join('?' * len(STAGES))})", (since, *STAGES)).fetchall()
    for ts, weapon, region in rows:
        cat = category(weapon)
        if cat and region not in _SKIP_REGIONS:
            out.add((cat, region, int(ts) // BUCKET_S))
    return out


def _count(sigs, lo: int, hi: int) -> dict[str, int]:
    c = {k: 0 for k in CATS}
    for cat, _, b in sigs:
        if lo <= b * BUCKET_S < hi:
            c[cat] += 1
    c["total"] = sum(c.values())
    return c


def stats_payload(db, now: int | None = None) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    today0 = kyiv_midnight_ts(now)
    sigs = _signals(db, today0 - 30 * 86400)
    periods = {
        "today": _count(sigs, today0, now + 1),
        "yesterday": _count(sigs, today0 - 86400, today0),
        "7d": _count(sigs, now - 7 * 86400, now + 1),
        "30d": _count(sigs, now - 30 * 86400, now + 1),
    }
    days = []
    for n in range(14):
        lo = today0 - n * 86400
        hi = lo + 86400
        tss = sorted(b * BUCKET_S for _, _, b in sigs if lo <= b * BUCKET_S < hi)
        cats = _count(sigs, lo, hi)
        regs: dict[str, int] = {}
        for _, r, b in sigs:
            if lo <= b * BUCKET_S < hi:
                regs[r] = regs.get(r, 0) + 1
        days.append({
            "date": kyiv_date(lo + 3600), "cats": cats, "total": cats["total"],
            "first": kyiv_fmt(tss[0]) if tss else "", "last": kyiv_fmt(tss[-1] + BUCKET_S) if tss else "",
            "regions": [region_name(r) for r, _ in sorted(regs.items(), key=lambda x: -x[1])[:3]],
        })
    hours = [0] * 24
    regions: dict[str, int] = {}
    for _, r, b in sigs:
        ts = b * BUCKET_S
        if ts >= now - 7 * 86400:
            hours[kyiv_hour(ts)] += 1
            regions[r] = regions.get(r, 0) + 1
    top = [{"slug": s, "name": region_name(s), "n": n} for s, n in sorted(regions.items(), key=lambda x: -x[1])[:10]]
    active = sorted({r for _, r, b in sigs if b * BUCKET_S >= now - 1800})
    return {"now": now, "periods": periods, "days": days, "hours": hours, "regions": top,
            "active_regions": active, "cats": {k: {"name": v[0], "icon": v[1]} for k, v in CATS.items()},
            "note": "Сигнали = унікальні (вид, область, 30 хв) зі зведень каналів, а не реєстр пусків."}
