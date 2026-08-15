"""Conservative fact extraction for linking operational message updates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from regions import detect_region

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


def _number(text: str) -> int | None:
    match = re.search(r"(?<!\w)(\d{1,3})(?!\w)", text)
    if match:
        return int(match.group(1))
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
            return value
    return None


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
    for marker in (" з ", " із ", " из ", " from "):
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
    # An unknown class is useful only when a source explicitly names a token.
    raw = ""
    if weapon_class == "unknown":
        match = re.search(r"[«\"]([^»\"]{2,40})[»\"]", text)
        raw = match.group(1).strip() if match else ""
    return IncidentFact(weapon_class, stage, origin, destination, kind, value, is_delta, raw)
