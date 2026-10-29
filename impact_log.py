"""impact_log.py — наслідки: що сталося, де, які жертви.

Принципи:
* лише офіційні джерела (OFFICIAL_SOURCES / IMPACT_SOURCES): зведення тривог і донат-пости
  наслідками не є;
* лише факти з тексту: кількість загиблих/поранених береться з явних чисел, інакше не заповнюється;
* без адрес і назв об'єктів: місто/область і загальна категорія («житлові будинки», «енергетика»);
* публікація із затримкою IMPACT_DELAY_MIN (за замовчуванням 180 хв): точки влучань у реальному
  часі допомагають коригувати вогонь, тому у стрічку і на карту вони не потрапляють.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from kyiv_time import fmt as kyiv_fmt
from regions import region_name

DEFAULT_DELAY_S = 180 * 60

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
_IMPACT_HINT = re.compile(r"наслідк|загинул|загиб|поранен|постраждал|жертв|пошкоджен|зруйнов|влучанн|приліт|прилетіл|ураж", re.I)


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
    """{killed, injured, damage[]} из явных чисел/слов; None, если пост — не про наслідки."""
    if not _IMPACT_HINT.search(text):
        return None
    killed = _first_number(_KILLED, text) or (1 if _ONE_KILLED.search(text) else None)
    injured = _first_number(_INJURED, text) or (1 if _ONE_INJURED.search(text) else None)
    damage = [name for name, rx in _DAMAGE if rx.search(text)]
    if not (killed or injured or damage):
        return None
    return {"killed": killed, "injured": injured, "damage": damage}


_CAP_WORD = re.compile(r"(?<![\w'’])[А-ЯІЇЄҐ][а-яіїєґ'’\-]{3,}(?:[\s-][А-ЯІЇЄҐ][а-яіїєґ'’\-]{2,})?")


def _where(text: str, parsed) -> tuple[str, str]:
    """(область, населений пункт) із тексту: пост про наслідки часто без зброї, тож парсер спостережень порожній."""
    from threat_parser import locate, oblast_in
    obs = [o for o in parsed.observations if o.relation != "from"]
    oblast = next((o.oblast for o in obs if o.oblast), "") or (oblast_in(text) or "")
    place = next((o.place for o in obs if o.place and o.precision == "place"), "")
    if not place:
        for m in _CAP_WORD.finditer(text):
            geo = locate(m.group(0), oblast) if oblast else locate(m.group(0), "")
            if geo and geo.precision == "place":
                place, oblast = geo.name, oblast or geo.oblast
                break
    return oblast, place


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS impact_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, event_ts INTEGER NOT NULL, "
            "source TEXT NOT NULL, oblast TEXT NOT NULL DEFAULT '', place TEXT NOT NULL DEFAULT '', "
            "killed INTEGER, injured INTEGER, damage TEXT NOT NULL DEFAULT '', fp TEXT NOT NULL UNIQUE)")
        db._conn.execute("CREATE INDEX IF NOT EXISTS idx_impact_ts ON impact_reports(event_ts)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def record_from_post(db, text: str, source: str, event_ts: int, parsed, allowed: frozenset[str]) -> bool:
    """Сохранить наслідки из поста официального источника. True — запись добавлена."""
    if source.lstrip("@").lower() not in allowed:
        return False
    info = extract_impact(text)
    if not info:
        return False
    oblast, place = _where(text, parsed)
    if not oblast and not place:
        return False
    fp = hashlib.sha256(f"{oblast}|{place}|{info['killed']}|{info['injured']}|{','.join(info['damage'])}|{event_ts // 3600}".encode()).hexdigest()
    ensure(db)
    try:
        with db._lock:  # noqa: SLF001
            cur = db._conn.execute(  # noqa: SLF001
                "INSERT OR IGNORE INTO impact_reports(event_ts, source, oblast, place, killed, injured, damage, fp) VALUES (?,?,?,?,?,?,?,?)",
                (int(event_ts), source, oblast, place, info["killed"], info["injured"], ",".join(info["damage"]), fp))
            db._conn.commit()  # noqa: SLF001
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False


def impacts_payload(db, now: int | None = None, delay_s: int = DEFAULT_DELAY_S, days: int = 14) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT event_ts, oblast, place, killed, injured, damage FROM impact_reports "
            "WHERE event_ts<=? AND event_ts>=? ORDER BY event_ts DESC LIMIT 200", (now - delay_s, now - days * 86400)).fetchall()
    items = [{"ts": ts, "when": kyiv_fmt(ts, "%d.%m %H:%M"), "oblast": region_name(ob) if ob else "", "place": pl,
              "killed": k, "injured": i, "damage": [d for d in dm.split(",") if d]} for ts, ob, pl, k, i, dm in rows]
    return {"now": now, "delay_min": delay_s // 60, "items": items,
            "totals": {"killed": sum(x["killed"] or 0 for x in items), "injured": sum(x["injured"] or 0 for x in items)},
            "note": "Лише повідомлення офіційних джерел, із затримкою і без адрес та назв об'єктів. Числа — з тексту повідомлень, можуть уточнюватись."}
