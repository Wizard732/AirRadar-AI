#!/usr/bin/env python3
"""Быстро собрать высокоточный silver-датасет без генерации LLM.

В набор попадают лишь однозначные оперативные посты. Целевой текст строится
только из явно названных фактов, поэтому он безопаснее разметки всего архива
маленькой языковой моделью.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from import_history import parse_html_file
from training_filter import normalize_source_text

DEFAULT_SOURCE = r"C:\Users\bvdov\Desktop\данные для обучения"
DEFAULT_OUTPUT = Path("training_data/silver")

_URL_OR_PROMO = re.compile(r"(?:https?://|t\.me/|@\w+|підпис|подпис|збір|сбор|донат|банка)", re.I)
_UNCERTAIN = re.compile(r"\b(?:можливо|ймовірно|вероятно|может быть|очікується|очікуван)\b", re.I)
_MULTI_EVENT = re.compile(r"(?:\n|[.!?])\s*(?:\d{1,2}:\d{2}\s*)?(?:повітряна\s+тривога|відбій|вибух|обстріл)", re.I)

# Формы оставлены как написаны в посте, чтобы не привносить неверный падеж города.
_WEAPONS: tuple[tuple[str, str], ...] = (
    # Shahed: полные названия, «шах», Герань, Гербера и Бандероль.
    (r"\b(?:реактивн\w*\s+)?(?:шахед\w*|шах\w*|shahed\w*|герань\w*|геран\w*|гербер\w*|бандерол\w*)\b", "БпЛА типу Shahed"),
    (r"\b(?:бпла|бпл-а|uav|дрон\w*)\b", "БпЛА"),
    (r"\b(?:каб\w*|фаб\w*|авіабомб\w*)\b", "КАБ"),
    (r"\b(?:балістик\w*|кинжал\w*|кінжал\w*|іскандер\w*|искандер\w*)\b", "Балістична ракета"),
    (r"\b(?:ракет\w*|калібр\w*|калибр\w*|циркон\w*|oniks\w*|онікс\w*|оникс\w*)\b", "Ракета"),
    (r"\b(?:рсзо|град\w*|смерч\w*|ураган\w*)\b", "РСЗВ"),
    (r"\b(?:артилер\w*|артилл\w*|міномет\w*|миномет\w*)\b", "Артилерія"),
)


def dedup_key(text: str) -> str:
    normalized = re.sub(r"[^\wіїєґа-я]+", "", text.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def sentence(text: str) -> str:
    return text.rstrip(" .!…") + "."


def extract_weapon(text: str) -> tuple[str, bool] | None:
    for pattern, label in _WEAPONS:
        match = re.search(pattern, text, re.I)
        if match:
            return label, "реактив" in match.group(0).lower()
    return None


def location_tail(text: str) -> str | None:
    """Достаёт явную цель после курсом/на/у напрямку; не угадывает географию."""
    match = re.search(
        r"\b(?:курсом\s+на|у\s+напрямку|в\s+направлении|напрямком\s+на|прямує\s+на|летить\s+на|летит\s+на)\s+"
        r"([А-ЯІЇЄҐA-Z][\w'’.-]*(?:\s+[А-ЯІЇЄҐA-Z][\w'’.-]*){0,3})",
        text,
        re.I,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" ,.!?—–")
    return value if 2 <= len(value) <= 45 else None


def make_target(text: str) -> tuple[str, str] | None:
    """Вернуть (event_type, target) лишь для жёстко однозначных форм."""
    lowered = text.lower()
    if _URL_OR_PROMO.search(text) or _UNCERTAIN.search(text) or _MULTI_EVENT.search(text):
        return None

    # Отбой проверяем раньше тревоги.
    alert_location = re.search(r"\b(?:відбій|отбой)\s+(?:повітряної\s+)?(?:тривоги|тревоги)\s+(?:в|у)\s+([^.!?]{2,45})", text, re.I)
    if alert_location:
        return "active_threat", sentence(f"Відбій повітряної тривоги в {alert_location.group(1).strip()}")

    alert_location = re.search(r"\b(?:повітряна\s+тривога|воздушная\s+тревога)\s+(?:в|у)\s+([^.!?]{2,45})", text, re.I)
    if alert_location:
        return "active_threat", sentence(f"Повітряна тривога в {alert_location.group(1).strip()}")

    weapon = extract_weapon(text)
    destination = location_tail(text)
    if weapon and destination:
        label, is_reactive = weapon
        prefix = "Реактивний " if is_reactive else ""
        return "active_threat", sentence(f"{prefix}{label} курсом на {destination}")

    # Явные подтверждённые последствия: место оставляем как в исходнике.
    impact = re.search(r"\b(?:вибух(?:и|ів)?|прильот\w*|влучан\w*|обстріл\w*)\s+(?:в|у|по)\s+([^.!?]{2,45})", text, re.I)
    if impact and (weapon or "обстр" in lowered):
        noun = re.search(r"\b(?:вибух(?:и|ів)?|прильот\w*|влучан\w*|обстріл\w*)", text, re.I)
        return "impact_or_shelling", sentence(f"{noun.group(0).capitalize()} в {impact.group(1).strip()}")

    ppo = re.search(r"\b(?:ппо|пво)\s+(?:працює|работает)\s+(?:в|у|над)\s+([^.!?]{2,45})", text, re.I)
    if ppo:
        return "impact_or_shelling", sentence(f"Працює ППО в {ppo.group(1).strip()}")
    return None


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать строго шаблонный silver-датасет AirRadar.")
    parser.add_argument("source", nargs="?", default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source, output = Path(args.source), args.output
    if not source.is_dir():
        parser.error(f"Папка не найдена: {source}")
    output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    for html_path in sorted(source.rglob("messages*.html")):
        channel = html_path.parent.name
        for timestamp, raw in parse_html_file(html_path):
            stats["total"] += 1
            cleaned = normalize_source_text(raw)
            target = make_target(cleaned)
            if target is None:
                stats["rejected"] += 1
                continue
            key = dedup_key(cleaned)
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            event_type, answer = target
            group = int(key[:8], 16) % 100
            split = "train" if group < 80 else "validation" if group < 90 else "test"
            rows.append({"split": split, "input": cleaned[:700], "target": answer,
                         "event_type": event_type, "channel": channel, "timestamp": timestamp,
                         "dedup_group": key[:16], "confidence": "high"})
            stats[f"accepted:{event_type}"] += 1

    for split in ("train", "validation", "test"):
        write_jsonl(output / f"{split}.jsonl", [row for row in rows if row["split"] == split])
    (output / "report.json").write_text(json.dumps({**stats, "accepted": len(rows)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Принято: {len(rows)}; train={sum(row['split'] == 'train' for row in rows)}; папка: {output}")


if __name__ == "__main__":
    main()
