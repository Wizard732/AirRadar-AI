"""impact_ai.py — нейромережа визначає, чи пост уже про НАСЛІДКИ (подія сталась), і витягує факти.

Нейромережа (summarizer.classify, OpenAI-сумісний провайдер) відповідає лише JSON за схемою. Захист від вигадок:
  * категорії пошкоджень — тільки з фіксованого списку;
  * числа загиблих/поранених приймаються лише якщо ця цифра (або числівник) справді є в тексті;
  * «загроза», «курс», «летить», «можливий приліт» — НЕ наслідки (is_impact=false);
  * при збої/невалідній відповіді повертається None — працює звичайний regex-розбір impact_log.
Місце (місто/область) визначає наявний геопарсер, а не ШІ.
"""
from __future__ import annotations

import json
import logging
import re

from airradar.security import ai_guard

logger = logging.getLogger(__name__)

DAMAGE_CATEGORIES = ["житлові будинки", "енергетика", "промисловість", "соціальна інфраструктура",
                     "критична інфраструктура", "транспорт", "пожежа"]

SYSTEM_PROMPT = (
    "Ти аналізуєш пост українського каналу про повітряні атаки. Визнач, чи пост описує НАСЛІДКИ атаки, що ВЖЕ СТАЛАСЬ "
    "(влучання, приліт, вибухи, загиблі/поранені, пошкодження). Загрози, курс цілей, «летить на…», «можливий приліт», "
    "відбій, реклама, новини про чужу територію — це НЕ наслідки.\n"
    "Відповідай ЛИШЕ одним JSON без пояснень: "
    '{"is_impact": true|false, "kind": "hit"|"blast"|"none", "killed": число|null, "injured": число|null, '
    '"children": число|null, "damage": [із списку: ' + ", ".join(DAMAGE_CATEGORIES) + "]}. "
    "kind=hit — влучання/приліт/жертви/пошкодження; kind=blast — лише чути вибухи. Числа — ТІЛЬКИ якщо вони прямо є в тексті, "
    "інакше null. Нічого не вигадуй."
)

_NUM_WORDS = ("один", "одна", "одного", "двоє", "два", "дві", "троє", "три", "четверо", "чотири", "п'ятеро", "п’ятеро", "п'ять", "п’ять")
_WORD_VAL = {"один": 1, "одна": 1, "одного": 1, "двоє": 2, "два": 2, "дві": 2, "троє": 3, "три": 3, "четверо": 4, "чотири": 4,
             "п'ятеро": 5, "п’ятеро": 5, "п'ять": 5, "п’ять": 5}


def _number_in_text(n: int, text: str) -> bool:
    """Число підтверджене текстом: цифрами або числівником; «людина/чоловік/жінка» дають 1."""
    if re.search(rf"(?<!\d){n}(?!\d)", text):
        return True
    low = text.lower()
    if any(w in low and _WORD_VAL[w] == n for w in _NUM_WORDS):
        return True
    return n == 1 and bool(re.search(r"людина|чоловік|жінка|дитина|один\b|одна\b", low))


def parse_response(raw: str, text: str) -> dict | None | bool:
    """JSON-відповідь → {kind,killed,injured,children,flags,damage}; False — ШІ каже «не наслідки»; None — відповідь непридатна."""
    ok, _ = ai_guard.validate_output(raw or "")
    if not ok:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("is_impact"), bool):
        return None
    if not d["is_impact"] or d.get("kind") not in ("hit", "blast"):
        return False

    def num(key):
        v = d.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not (0 < v <= 500):
            return None
        return v if _number_in_text(v, text) else None      # число, якого немає в тексті, відкидається

    damage = [x for x in (d.get("damage") or []) if x in DAMAGE_CATEGORIES] if isinstance(d.get("damage"), list) else []
    damage = list(dict.fromkeys(damage))
    if d["kind"] == "blast":
        return {"kind": "blast", "killed": None, "injured": None, "children": None, "flags": "", "damage": damage}
    return {"kind": "hit", "killed": num("killed"), "injured": num("injured"), "children": num("children"), "flags": "", "damage": damage}


def worth_asking(text: str) -> bool:
    """Дешевий префільтр, щоб не викликати ШІ на кожен пост: є слова про влучання/вибух/жертви/пошкодження."""
    import impact_log
    return bool(impact_log._HIT.search(text) or impact_log._BLAST.search(text))  # noqa: SLF001


async def classify_impact(summarizer, text: str) -> dict | None | bool:
    """None — ШІ недоступний/невалідно (використати regex); False — не наслідки; dict — факти."""
    if summarizer is None or not worth_asking(text):
        return None
    try:
        raw = await summarizer.classify(text[:1500], SYSTEM_PROMPT, max_tokens=220)
    except Exception:  # noqa: BLE001
        logger.warning("impact_ai: ШІ недоступний", exc_info=True)
        return None
    return parse_response(raw, text)
