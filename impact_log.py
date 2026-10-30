"""impact_log.py — наслідки: що сталося, де, які жертви.

Джерело — повідомлення моніторингових каналів, які пишуть про влучання одразу. Фільтр: потрібне слово про
влучання/вибух/жертви/пошкодження і місце (місто або область); донати, реклама, новини, чужа територія,
«прильотів немає» відсікаються. Числа жертв — лише з явних чисел у тексті. Адреси та назви об'єктів не
зберігаються: місце — місто/область, наслідки — загальна категорія («житлові будинки», «енергетика»).
Дані неофіційні і можуть уточнюватись.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from kyiv_time import fmt as kyiv_fmt
from regions import region_name

logger = logging.getLogger(__name__)
DEFAULT_DELAY_S = 0

_NUM_WORDS = {"один": 1, "одна": 1, "одного": 1, "двоє": 2, "два": 2, "дві": 2, "троє": 3, "три": 3,
              "четверо": 4, "чотири": 4, "п'ятеро": 5, "п’ятеро": 5, "п'ять": 5, "п’ять": 5}
_NUM = r"(\d{1,3}|" + "|".join(re.escape(w) for w in _NUM_WORDS) + r")"
_KILLED = [
    re.compile(_NUM + r"\s+(?:людей\s+|осіб\s+|цивільних\s+|мирних\s+)?(?:загинул\w*|загиблих|загиблі|загиб\w*|убит\w*)", re.I),
    re.compile(r"(?:загинул\w*|загиб\w*|убит\w*)[^\d\n.]{0,25}?" + _NUM + r"(?!\s*(?::|год|хв))", re.I),
    re.compile(r"жертв\w*[^\d\n.]{0,15}" + _NUM, re.I),
]
_INJURED = [
    re.compile(_NUM + r"\s+(?:людей\s+|осіб\s+|цивільних\s+)?(?:поранен\w*|травмован\w*|постраждал\w*)", re.I),
    re.compile(r"(?:поранен\w*|травмован\w*|постраждал\w*)[^\d\n.]{0,25}?" + _NUM + r"(?!\s*(?::|год|хв))", re.I),
]
_CHILDREN = [
    re.compile(r"(?:серед\s+(?:них|загиблих|постраждалих|поранених)[^\d\n.]{0,12})" + _NUM + r"\s+(?:дітей|дитин\w*|дітьми)", re.I),
    re.compile(_NUM + r"\s+(?:дітей|дитин\w*)\s+(?:загинул|поранен|постраждал)", re.I),
    re.compile(r"(?:загинул\w*|поранен\w*|постраждал\w*)[^\d\n.]{0,12}" + _NUM + r"\s+(?:дітей|дитин\w*)", re.I),
]
_KILLED_FLAG = re.compile(r"\bє\s+(?:загибл|жертв)|загинул|загиб|жертв", re.I)
_INJURED_FLAG = re.compile(r"\bє\s+(?:постраждал|поранен)|постраждал|поранен|травмован", re.I)
_ONE_KILLED = re.compile(r"(?:загинул[аи]?|загинув)\s+(?:людина|чоловік|жінка|дитина)|(?:людина|чоловік|жінка|дитина)\s+загинул", re.I)
_ONE_INJURED = re.compile(r"(?:постраждал[аи]?|поранен[аи]?)\s+(?:людина|чоловік|жінка|дитина)|(?:людина|чоловік|жінка|дитина)\s+(?:постраждал|поранен)", re.I)

_DAMAGE = [
    ("житлові будинки", re.compile(r"житлов|багатоповерхів|приватн\w+\s+будин|будинк|квартир", re.I)),
    ("енергетика", re.compile(r"енергет|підстанц|електро|лінії електро|ТЕЦ|ТЕС|ГЕС", re.I)),
    ("промисловість", re.compile(r"промислов|підприємств|завод|склад|виробнич", re.I)),
    ("соціальна інфраструктура", re.compile(r"шкіл|лікарн|дитяч\w+\s+садок|університет|поліклінік|заклад освіти", re.I)),
    ("критична інфраструктура", re.compile(r"критичн\w+\s+інфраструктур|інфраструктур", re.I)),
    ("транспорт", re.compile(r"залізнич|вокзал|міст\b|автівк|транспорт", re.I)),
    ("пожежа", re.compile(r"пожеж|займанн|палає|горить", re.I)),
]
_STRONG = re.compile(r"приліт|прилет|прильот|влучан|влучил|влучив|уражен|удар\s+по|ударил|зруйнов", re.I)
_BAD_PLACE = re.compile(r"україн|росі|білорус|громад|област|район|щин|щини|щину", re.I)
_HIT = re.compile(r"приліт|прилет|прильот|влучан|влучил|влучив|впав\b|впала\b|впали\b|упал|уражен|удар\s+по|ударил|зруйнов|пошкоджен|наслідк|загинул|загиб|поранен|постраждал|жертв", re.I)
_BLAST = re.compile(r"вибух|вибух\w*|взрыв", re.I)
_NOT_IMPACT = re.compile(r"прильот\w*\s+(?:не\s+було|немає)|без\s+(?:прильот|наслідк|постраждал)|не\s+було\s+(?:прильот|влучан)|наслідків\s+немає|"
                         r"збір\b|донат|зібрат|збираємо|реквізит|підтримк\w+\s+(?:збору|наших)|монобанк|банк[ао]", re.I)


def _to_int(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit():
        n = int(tok)
        return n if 0 < n <= 500 else None
    return _NUM_WORDS.get(tok)


def _first_number(patterns, text: str) -> int | None:
    for p in patterns:
        m = p.search(text)
        if m:
            n = _to_int(m.group(1))
            if n:
                return n
    return None


def extract_impact(text: str) -> dict[str, Any] | None:
    """{kind, killed, injured, damage[]} з явних слів/чисел; None, якщо пост не про наслідки.

    kind: 'hit' — влучання/приліт/жертви/пошкодження; 'blast' — лише «вибухи» (чутно, не обов'язково влучання)."""
    if _NOT_IMPACT.search(text):
        return None
    killed = _first_number(_KILLED, text) or (1 if _ONE_KILLED.search(text) else None)
    injured = _first_number(_INJURED, text) or (1 if _ONE_INJURED.search(text) else None)
    children = _first_number(_CHILDREN, text)
    flags = ("k" if killed is None and _KILLED_FLAG.search(text) else "") + ("i" if injured is None and _INJURED_FLAG.search(text) else "")
    damage = [name for name, rx in _DAMAGE if rx.search(text)]
    # «впав/впала» і розмиті «пошкоджено/наслідки» без деталей — не влучання, а лише падіння/вибух.
    hit = bool(_STRONG.search(text))
    if hit or killed or injured or flags or (damage and re.search(r"пошкоджен|зруйнов|пожеж|горить|палає", text, re.I)):
        return {"kind": "hit", "killed": killed, "injured": injured, "children": children, "flags": flags, "damage": damage}
    if _BLAST.search(text) or _HIT.search(text):
        return {"kind": "blast", "killed": None, "injured": None, "children": None, "flags": "", "damage": damage}
    return None


_CAP_WORD = re.compile(r"(?<![\w'’])[А-ЯІЇЄҐ][а-яіїєґ'’\-]{3,}(?:[\s-][А-ЯІЇЄҐ][а-яіїєґ'’\-]{2,})?")


def _where(text: str, parsed, source: str = ""):
    """(область, місто, lat, lon, точність): пост про наслідки часто без зброї, тож розбір спостережень порожній."""
    from regions import detect_region
    from threat_parser import locate, oblast_geo, oblast_in
    obs = [o for o in parsed.observations if o.relation != "from"]
    oblast = next((o.oblast for o in obs if o.oblast), "") or (oblast_in(text) or "")
    if not oblast:
        regs = [r for r in detect_region(text, channel=source) if r not in ("unknown", "multi")]
        oblast = regs[0] if regs else ""
    place, lat, lon, prec = "", None, None, "none"
    pl = next((o for o in obs if o.place and o.precision == "place" and not _BAD_PLACE.search(o.place)), None)
    if pl:
        place, lat, lon, prec = pl.place, pl.lat, pl.lon, "place"
        if pl.oblast:
            oblast = pl.oblast
    else:
        for m in _CAP_WORD.finditer(text):
            if _BAD_PLACE.search(m.group(0)):
                continue
            geo = locate(m.group(0), oblast or "")
            if geo and geo.precision == "place" and not _BAD_PLACE.search(geo.name):
                place, lat, lon, prec = geo.name, geo.lat, geo.lon, "place"
                oblast = geo.oblast or oblast
                break
    if prec == "none" and oblast:
        og = oblast_geo(oblast)
        if og:
            lat, lon, prec = og.lat, og.lon, "oblast"
    return oblast, place, lat, lon, prec


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        c = db._conn  # noqa: SLF001
        c.execute(
            "CREATE TABLE IF NOT EXISTS impact_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, event_ts INTEGER NOT NULL, "
            "source TEXT NOT NULL, oblast TEXT NOT NULL DEFAULT '', place TEXT NOT NULL DEFAULT '', "
            "killed INTEGER, injured INTEGER, damage TEXT NOT NULL DEFAULT '', fp TEXT NOT NULL UNIQUE)")
        have = {r[1] for r in c.execute("PRAGMA table_info(impact_reports)")}
        for col, ddl in (("children", "INTEGER"), ("flags", "TEXT NOT NULL DEFAULT ''"), ("lat", "REAL"), ("lon", "REAL"), ("kind", "TEXT NOT NULL DEFAULT 'hit'"), ("precision", "TEXT NOT NULL DEFAULT 'none'")):
            if col not in have:
                c.execute(f"ALTER TABLE impact_reports ADD COLUMN {col} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_impact_ts ON impact_reports(event_ts)")
        c.commit()


def record_from_post(db, text: str, source: str, event_ts: int, parsed, allowed: frozenset[str] | None = None) -> dict | None:
    """Зберегти наслідки з поста. Повертає запис (dict) для нового факту, інакше None.

    allowed — необов'язкове обмеження списком джерел (за замовчуванням приймаємо всі моніторингові канали)."""
    if allowed and source.lstrip("@").lower() not in allowed:
        return None
    if parsed.kind in ("news", "foreign", "stats", "promo"):
        return None
    info = extract_impact(text)
    if not info:
        return None
    oblast, place, lat, lon, prec = _where(text, parsed, source)
    if not oblast and not place:
        return None
    ensure(db)
    updated = False
    try:
        with db._lock:  # noqa: SLF001
            prev = None
            if info["kind"] == "hit" and (place or oblast):
                prev = db._conn.execute(  # noqa: SLF001
                    "SELECT id, killed, injured, children, flags, damage FROM impact_reports WHERE kind='hit' AND oblast=? AND place=? "
                    "AND event_ts>=? ORDER BY event_ts DESC LIMIT 1", (oblast, place, int(event_ts) - 6 * 3600)).fetchone()
            if prev is not None:
                # Той самий інцидент: нове повідомлення з більшими числами/новими деталями оновлює запис, інше — дубль.
                pk, pkill, pinj, pch, pflags, pdmg = prev
                nk = max(info["killed"] or 0, pkill or 0) or None
                ni = max(info["injured"] or 0, pinj or 0) or None
                nc = max(info["children"] or 0, pch or 0) or None
                dmg = list(dict.fromkeys([d for d in pdmg.split(",") if d] + info["damage"]))
                flags = "".join(f for f in ("k", "i") if f in (pflags or "") + info["flags"] and (nk if f == "k" else ni) is None)
                if (nk, ni, nc, tuple(dmg)) == (pkill, pinj, pch, tuple(d for d in pdmg.split(",") if d)) and flags == (pflags or ""):
                    return None
                db._conn.execute("UPDATE impact_reports SET killed=?, injured=?, children=?, flags=?, damage=?, event_ts=? WHERE id=?",  # noqa: SLF001
                                 (nk, ni, nc, flags, ",".join(dmg), int(event_ts), pk))
                db._conn.commit()  # noqa: SLF001
                updated = True
                info = {**info, "killed": nk, "injured": ni, "children": nc, "flags": flags, "damage": dmg}
            else:
                fp = hashlib.sha256(f"{oblast}|{place}|{info['kind']}|{info['killed']}|{info['injured']}|{int(event_ts) // 1800}".encode()).hexdigest()
                cur = db._conn.execute(  # noqa: SLF001
                    "INSERT OR IGNORE INTO impact_reports(event_ts, source, oblast, place, killed, injured, damage, fp, lat, lon, kind, precision, children, flags) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(event_ts), source, oblast, place, info["killed"], info["injured"], ",".join(info["damage"]), fp, lat, lon,
                     info["kind"], prec, info["children"], info["flags"]))
                db._conn.commit()  # noqa: SLF001
                if cur.rowcount <= 0:
                    return None
    except Exception:  # noqa: BLE001
        logger.exception("impact_log: запис не вдався")
        return None
    return {"ts": int(event_ts), "oblast": oblast, "place": place, "lat": lat, "lon": lon, "precision": prec, "updated": updated, **info}


def casualty_line(killed, injured, children=None, flags: str = "") -> list[str]:
    out = []
    if killed:
        out.append(f"🔴 Загинули: {killed}")
    elif "k" in (flags or ""):
        out.append("🔴 Є загиблі (кількість уточнюється)")
    if injured:
        out.append(f"🟠 Поранені: {injured}")
    elif "i" in (flags or ""):
        out.append("🟠 Є постраждалі (кількість уточнюється)")
    if children:
        out.append(f"👶 Серед постраждалих дітей: {children}")
    return out


def impact_post_text(rec: dict) -> str:
    """Коротке повідомлення для каналу про влучання (лише kind='hit'); при уточненні — «ОНОВЛЕННЯ»."""
    where = rec["place"] or (region_name(rec["oblast"]) if rec["oblast"] else "")
    oblast = f" ({region_name(rec['oblast'])})" if rec["place"] and rec["oblast"] else ""
    title = "💥 ОНОВЛЕННЯ: ВЛУЧАННЯ" if rec.get("updated") else "💥 ВЛУЧАННЯ"
    lines = [f"{title} — {where.upper()}{oblast}", f"🕒 {kyiv_fmt(rec['ts'])}"]
    lines += casualty_line(rec["killed"], rec["injured"], rec.get("children"), rec.get("flags", ""))
    if rec["damage"]:
        lines.append("🏚 " + ", ".join(rec["damage"]))
    lines += ["", "Дані зі зведень каналів, неофіційні, можуть уточнюватись.", "📡 Джерело: AirRadar AI"]
    return "\n".join(lines)


def impacts_payload(db, now: int | None = None, delay_s: int = DEFAULT_DELAY_S, days: int = 14) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT event_ts, oblast, place, killed, injured, damage, lat, lon, kind, precision, children, flags FROM impact_reports "
            "WHERE event_ts<=? AND event_ts>=? ORDER BY event_ts DESC LIMIT 300", (now - delay_s, now - days * 86400)).fetchall()
    items = [{"ts": ts, "when": kyiv_fmt(ts, "%d.%m %H:%M"), "oblast": region_name(ob) if ob else "", "slug": ob, "place": pl,
              "killed": k, "injured": i, "children": ch, "flags": fl or "", "damage": [d for d in dm.split(",") if d],
              "lat": lat, "lon": lon, "kind": kind, "precision": pr}
             for ts, ob, pl, k, i, dm, lat, lon, kind, pr, ch, fl in rows]
    hits = [x for x in items if x["kind"] == "hit"]
    return {"now": now, "delay_min": delay_s // 60, "items": items,
            "totals": {"killed": sum(x["killed"] or 0 for x in hits), "injured": sum(x["injured"] or 0 for x in hits),
                       "children": sum(x["children"] or 0 for x in hits), "hits": len(hits),
                       "unclear": sum(1 for x in hits if x["flags"])},
            "note": "Дані зі зведень каналів: неофіційні, можуть уточнюватись. Адреси й назви об'єктів не публікуються."}
