"""alarm_notices.py — пости про початок і кінець повітряної тривоги.

Джерела: (1) офіційні сирени alerts.in.ua (якщо задано ALERTS_IN_UA_TOKEN) — найнадійніше;
(2) строгий розбір постів каналів: «Повітряна тривога в <область>» / «Відбій тривоги в <область>».
Балаканина («по всьому тиша», «відбій давати не хочуть», «відбій загрози балістики») тривогою не вважається.
Стан по областях зберігається, тож кожна зміна публікується один раз, а відбій несе тривалість.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Awaitable, Callable

from kyiv_time import fmt as kyiv_fmt
from regions import detect_region, region_name

logger = logging.getLogger(__name__)

MAX_AGE_S = 15 * 60          # старші пости (дозавантаження історії) не публікуємо
STALE_OPEN_S = 3 * 3600      # «відкрита» тривога без підтвердження довше — вважаємо застарілою

_START = re.compile(r"повітряна\s+тривога|оголошен\w+\s+(?:повітряну\s+)?тривог|тривога\s+(?:в|у|на)\s", re.I)
_END = re.compile(r"відбій\s+(?:повітряної\s+)?тривоги|відбій\s+(?:в|у|на)\s|відбій\s+повітряної", re.I)
_NEG = re.compile(r"тиш[аі]|спокійн|по\s+всьому\s+чисто|\bчисто\b|нема\w*\s+(?:тривог|відбою)|немає\s+(?:тривог|відбою)|без\s+(?:тривог|відбою)|"
                  r"відбій[^.\n]{0,25}(?:не\s+хоч|давати\s+не|не\s+буде)|відбою\s+не|скасов|не\s+оголош|станом\s+на\s+\d", re.I)
_BALLISTIC_END = re.compile(r"відбій\s+загрози", re.I)

Notify = Callable[[list[str], str], Awaitable[None]]


def classify(text: str, source: str = "") -> tuple[str, list[str]]:
    """('start'|'end'|'', [slug…]) — лише коли пост явно про тривогу/відбій і названа область."""
    if _NEG.search(text) or _BALLISTIC_END.search(text):
        return "", []
    kind = "end" if _END.search(text) else "start" if _START.search(text) else ""
    if not kind:
        return "", []
    regs = [r for r in detect_region(text, channel=source) if r not in ("unknown", "multi")]
    if not regs or len(regs) > 6:
        return "", []
    return kind, regs


def suppress_old_standdown(text: str, source: str = "") -> bool:
    """Старий пост «🟢 ВІДБІЙ» не публікуємо, якщо це балаканина або регіональний відбій, який оформлює цей модуль."""
    if _NEG.search(text):
        return True
    return classify(text, source)[0] == "end"


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS alarm_state (region TEXT PRIMARY KEY, open INTEGER NOT NULL DEFAULT 0, "
            "since_ts INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL DEFAULT 0)")
        db._conn.commit()  # noqa: SLF001


def _names(slugs: list[str]) -> str:
    return ", ".join(region_name(s) for s in slugs)


def start_text(slugs: list[str], ts: int) -> str:
    return f"🚨 ПОВІТРЯНА ТРИВОГА\n📍 {_names(slugs)}\n🕒 {kyiv_fmt(ts)}\n\nПрямуйте в укриття.\n📡 Джерело: AirRadar AI"


def end_text(slugs: list[str], ts: int, minutes: int | None) -> str:
    dur = f" · тривала {minutes} хв" if minutes else ""
    return f"🟢 ВІДБІЙ ТРИВОГИ\n📍 {_names(slugs)}\n🕒 {kyiv_fmt(ts)}{dur}\n📡 Джерело: AirRadar AI"


async def apply(db, publisher, notify: Notify | None, kind: str, slugs: list[str], ts: int, now: int | None = None) -> str:
    """Застосувати зміну стану; публікує один пост на пакет областей. Повертає 'start'|'end'|'' ."""
    now = int(now if now is not None else time.time())
    if now - ts > MAX_AGE_S:
        return ""
    _ensure(db)
    changed: list[str] = []
    durations: list[int] = []
    with db._lock:  # noqa: SLF001
        for slug in dict.fromkeys(slugs):
            row = db._conn.execute("SELECT open, since_ts, last_ts FROM alarm_state WHERE region=?", (slug,)).fetchone()  # noqa: SLF001
            is_open = bool(row and row[0] and ts - row[2] <= STALE_OPEN_S)
            if kind == "start":
                db._conn.execute("INSERT INTO alarm_state(region, open, since_ts, last_ts) VALUES (?,1,?,?) "  # noqa: SLF001
                                 "ON CONFLICT(region) DO UPDATE SET open=1, last_ts=excluded.last_ts, "
                                 "since_ts=CASE WHEN alarm_state.open=1 AND ?-alarm_state.last_ts<=? THEN alarm_state.since_ts ELSE excluded.since_ts END",
                                 (slug, ts, ts, ts, STALE_OPEN_S))
                if not is_open:
                    changed.append(slug)
            elif is_open:
                durations.append(max(1, round((ts - row[1]) / 60)))
                db._conn.execute("UPDATE alarm_state SET open=0, last_ts=? WHERE region=?", (ts, slug))  # noqa: SLF001
                changed.append(slug)
        db._conn.commit()  # noqa: SLF001
    if not changed:
        return ""
    text = start_text(changed, ts) if kind == "start" else end_text(changed, ts, max(durations) if durations else None)
    try:
        await publisher.send(text)
    except Exception:  # noqa: BLE001
        logger.exception("alarm_notices: не вдалося опублікувати")
    if notify is not None:
        try:
            await notify(changed, text)
        except Exception:  # noqa: BLE001
            logger.exception("alarm_notices: розсилка підписникам не вдалась")
    return kind


async def handle_post(db, publisher, notify: Notify | None, text: str, source: str, event_ts: int) -> str:
    kind, slugs = classify(text, source)
    if not kind:
        return ""
    return await apply(db, publisher, notify, kind, slugs, event_ts)
