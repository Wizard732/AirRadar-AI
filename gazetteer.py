"""gazetteer.py — справочник населённых пунктов Украины (GeoNames, ~51 тыс. названий).

Названия в постах стоят в любом падеже («на Дружківку», «біля Павлограда»), а
в справочнике — в именительном. Сравниваем по основе: у слова отрезается самое
длинное падежное окончание из списка, ту же операцию делаем со справочником.
Одинаковые названия в разных областях (сотни «Іванівок») разводятся областью из
поста; если и в области совпадений несколько — возвращаем всех кандидатов, а
трекер выберет ближайшего к предыдущей позиции цели.
"""

from __future__ import annotations

import gzip
import os
import re
from dataclasses import dataclass

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gazetteer_ua.tsv.gz")
_ENDINGS = tuple(sorted(
    ("ому", "ого", "ами", "ями", "ові", "еві", "єві", "ою", "ею", "єю", "ом", "ем", "єм", "ам", "ям",
     "ів", "їв", "ах", "ях", "ої", "ій", "а", "я", "и", "і",
     "у", "ю", "е", "є", "о", "ь", "й", "ї"),
    key=len, reverse=True,
))
_APOS = re.compile(r"[ʼ’`´‘']")


@dataclass(frozen=True)
class Candidate:
    name: str
    oblast: str
    lat: float
    lon: float
    population: int


def _norm_word(word: str) -> str:
    return _APOS.sub("", word.lower().replace("ё", "е")).strip(" .,:;!?-–—")


def stem_word(word: str, min_len: int = 4) -> str:
    word = _norm_word(word)
    for ending in _ENDINGS:
        if len(word) - len(ending) >= min_len and word.endswith(ending):
            return word[: -len(ending)]
    return word


def stem_key(name: str) -> str:
    parts = [w for w in re.split(r"[\s]+", name) if w]
    # В составных названиях («Біла Церква», «Нова Каховка») прилагательное тоже склоняется,
    # а оно короткое: «білу»/«біла» должны дать одну основу.
    min_len = 3 if len(parts) > 1 else 4
    return " ".join(stem_word(w, min_len) for w in parts)


_INDEX: dict[str, list[Candidate]] | None = None


def _load() -> dict[str, list[Candidate]]:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    index: dict[str, list[Candidate]] = {}
    if os.path.exists(_PATH):
        with gzip.open(_PATH, "rt", encoding="utf-8") as fh:
            for line in fh:
                name, oblast, lat, lon, pop = line.rstrip("\n").split("\t")
                index.setdefault(stem_key(name), []).append(
                    Candidate(name, oblast, float(lat), float(lon), int(pop)))
    for items in index.values():
        items.sort(key=lambda c: -c.population)
    _INDEX = index
    return index


def find(name: str, oblast: str = "") -> list[Candidate]:
    """Кандидаты по названию (самые крупные первыми). С областью — только её,
    без области — вся страна. Пусто, если названия нет в справочнике."""
    words = re.split(r"\s+", name.strip())
    key = stem_key(name)
    found = list(_load().get(key, ()))
    if not found and len(words) > 1:
        # «м. Сміла», «село Жуки»: отбрасываем служебное слово
        found = list(_load().get(stem_key(" ".join(words[1:])), ()))
    if oblast:
        found = [c for c in found if c.oblast == oblast]
    return found


_TOWNS: list[Candidate] | None = None


def towns(min_population: int = 3000) -> list[Candidate]:
    """Крупные пункты (для «через какие города пролетит»); дубли по координатам схлопнуты."""
    global _TOWNS
    if _TOWNS is None:
        seen: dict[tuple[float, float], Candidate] = {}
        for items in _load().values():
            for c in items:
                if c.population >= 2000:
                    seen.setdefault((round(c.lat, 2), round(c.lon, 2)), c)
        _TOWNS = sorted(seen.values(), key=lambda c: -c.population)
    return [c for c in _TOWNS if c.population >= min_population]
