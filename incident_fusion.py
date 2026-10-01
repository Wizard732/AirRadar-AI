"""Conservative fact extraction for linking operational message updates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from regions import detect_region
from weapon_classes import detect_speed_profile

_NUMBER_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "дві": 2,
    "три": 3, "чотири": 4, "четыре": 4, "пять": 5, "п'ять": 5, "п’ять": 5,
    "шість": 6, "шесть": 6, "сім": 7, "семь": 7, "вісім": 8, "восемь": 8,
    "дев'ять": 9, "дев’ять": 9, "девять": 9, "десять": 10,
}


@dataclass(frozen=True)
class IncidentFact:
    weapon_class: str
    stage: str
    origin_region: str
    destination_region: str
    count_kind: str
    count_value: int | None
    is_delta: bool
    raw_designation: str
    # Профиль скорости подтипа ("" | "reactive"): класс остаётся uav/shahed,
    # но реактивный летит втрое быстрее — ETA/трек считаются по профилю.
    # Дефолт сохраняет совместимость с прямыми конструкторами в тестах.
    speed_profile: str = ""


def _number(text: str) -> int | None:
    """Extract a count only when a number explicitly counts a threat object."""
    objects = r"(?:бпла|дрон(?:ів|и|а)?|шахед(?:ів|и|а)?|ракет(?:а|и|и)?|ціл(?:ь|і|ей))"
    match = re.search(rf"(?<!\w)(\d{{1,3}})\s+{objects}(?!\w)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Elliptical updates such as «ще 2 на Київ» may omit the object, but
    # explicit additive language prevents model/designation numbers matching.
    delta = re.search(r"\b(?:ще|ещё|додатково|another|more)\s+(\d{1,3})(?!\w)", text, re.IGNORECASE)
    if delta:
        return int(delta.group(1))
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(word)}\s+{objects}(?!\w)", text, re.IGNORECASE):
            return value
    return None


def _destinations(text: str) -> list[str]:
    """Все явные цели по маркерам направления (в порядке появления текста).

    Сводки мониторинговых каналов перечисляют несколько направлений
    («…на Васильків … на Глобине … на Сергіївку»). Заголовок по одному
    «последнему совпадению» дезинформирует, поэтому сборный пост должен
    помечаться как мультирегиональный.
    """
    lowered = text.lower()
    found: list[str] = []
    for marker in _ROUTE_MARKERS:
        start = 0
        while True:
            pos = lowered.find(marker, start)
            if pos < 0:
                break
            start = pos + len(marker)
            for slug in detect_region(text[start:]):
                if slug not in found:
                    found.append(slug)
                break  # первая область в суффиксе — цель этого маркера
    return found


_ROUTE_MARKERS = (" на ", " до ", " у напрямку ", " в направлении ", " towards ")


def _is_roundup(text: str, destinations: list[str]) -> bool:
    """Сборная сводка: несколько направлений и 3+ области в тексте.

    Цели-города (Васильків, Глобине) могут отсутствовать в словаре — тогда
    явных целей мало, но областных упоминаний много. Такой пост нельзя
    подписывать одним «последним» регионом.
    """
    if len(destinations) >= 2:
        return True
    markers = sum(text.lower().count(m) for m in _ROUTE_MARKERS)
    return markers >= 2 and len(set(detect_region(text))) >= 3


def _route_regions(text: str) -> tuple[str, str]:
    """Use the region after a destination marker as target, never blindly first region."""
    lowered = text.lower()
    all_regions = detect_region(text)
    destination = ""
    for marker in (" на ", " до ", " у напрямку ", " в направлении ", " towards "):
        pos = lowered.rfind(marker)
        if pos >= 0:
            suffix_regions = detect_region(text[pos + len(marker):])
            if suffix_regions:
                destination = suffix_regions[0]
                break
    origin = ""
    for marker in (" з ", " зі ", " із ", " из ", " from "):
        pos = lowered.find(marker)
        if pos >= 0:
            segment = text[pos + len(marker):]
            stop = re.search(r"\s+(?:на|до|у напрямку|в направлении|towards)\b", segment.lower())
            if stop:
                segment = segment[:stop.start()]
            prefix_regions = detect_region(segment)
            if prefix_regions:
                origin = prefix_regions[0]
            elif re.search(r"(?<!\w)сум(?!\w)", segment.lower()):
                # Genitive form «з Сум» is common and is not in the generic index.
                origin = "sumska"
            if origin:
                break
    return origin, destination or (all_regions[-1] if all_regions else "unknown")


def extract_incident_fact(text: str, weapon_class: str, stage: str) -> IncidentFact:
    """Extract only explicit count/route facts; ambiguity stays unspecified."""
    lowered = text.lower()
    value = _number(lowered)
    is_delta = bool(re.search(r"\b(ще|ещё|додатково|another|more)\b", lowered))
    if is_delta and value is not None:
        kind = "delta"
    elif value is not None and re.search(r"\b(щонайменше|не менше|at least)\b", lowered):
        kind = "at_least"
    elif value is not None and re.search(r"\b(до|не більше|up to)\b", lowered):
        kind = "at_most"
    elif value is not None:
        kind = "exact"
    elif re.search(r"\b(група|группа|кілька|несколько|багато|many)\b", lowered):
        kind = "vague"
    else:
        kind = "unspecified"
    origin, destination = _route_regions(text)
    # Сборный пост (сводка по нескольким областям): честная пометка вместо
    # случайного «последнего» региона в заголовке.
    if _is_roundup(text, _destinations(text)):
        destination = "multi"
        origin = ""
    # An unknown class is useful only when a source explicitly names a token.
    raw = ""
    if weapon_class == "unknown":
        match = re.search(r"[«\"]([^»\"]{2,40})[»\"]", text)
        raw = match.group(1).strip() if match else ""
    return IncidentFact(
        weapon_class, stage, origin, destination, kind, value, is_delta, raw,
        speed_profile=detect_speed_profile(text, weapon_class),
    )
