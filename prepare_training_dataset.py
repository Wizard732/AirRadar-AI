#!/usr/bin/env python3
"""Подготовить кандидатов и финальный JSONL-датасет из HTML-экспорта Telegram.

Режим ``candidates`` ничего не отправляет в LLM: он создаёт компактный файл для
разметки. Режим ``finalize`` проверяет уже размеченный LLM JSONL и делит его на
train/validation/test без утечки дублей между наборами.

Примеры:
  python prepare_training_dataset.py candidates "C:/.../данные для обучения"
  python prepare_training_dataset.py finalize training_data/llm_labels.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from import_history import parse_html_file
from training_filter import classify_for_training

DEFAULT_SOURCE = r"C:\Users\bvdov\Desktop\данные для обучения"
DEFAULT_OUTPUT = Path("training_data")
MAX_SOURCE_LENGTH = 700
MAX_TARGET_WORDS = 28
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)

# Обязательный контракт для разметчика (Ollama/Groq или ручной разметки).
LABEL_PROMPT = """Ти розмічаєш Telegram-пости для датасету повітряних загроз України.
Поверни ТІЛЬКИ один JSON-об'єкт за схемою:
{"keep":true,"event_type":"active_threat|impact_or_shelling|irrelevant","target":"коротке українське повідомлення","reject_reason":""}

Правила:
- keep=true лише для поточної повітряної/ракетної/дронової загрози, обстрілу,
  вибуху, влучання або роботи ППО.
- Відкинь рекламу, збори, підписки, політику, аналітику, загальні зведення,
  прогнози та все, що не описує конкретну воєнну подію.
- event_type: РІВНО одне значення: active_threat АБО impact_or_shelling АБО irrelevant.
  Заборонено поєднувати значення через |, /, кому чи будь-яким іншим способом.
- target: одне сухе речення українською, максимум 28 слів, без емодзі, URL, хештегів,
  закликів, подяк, оцінок, лайки й припущень. Прибери «можливо», «ймовірно»,
  «чекаємо», «залишайтеся в укриттях», «дякуємо ППО» та подібну воду.
  Передавай лише підтверджений факт з поста.
- Не вигадуй тип зброї, місто, кількість, час, маршрут або наслідки.
- «Чути мопед», «схоже на мопед» або інше слухове/візуальне враження не є
  підтвердженням Shahed чи БпЛА: збережи його як спостереження або відкинь.
- Нормалізуй відомі назви: «шахед» -> «БпЛА типу Shahed», «каби» -> «КАБ».
- Приклад: «реактивний шахед курсом на суми» ->
  {"keep":true,"event_type":"active_threat","target":"Реактивний БпЛА типу Shahed курсом на Суми.","reject_reason":""}
"""


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def canonical_key(text: str) -> str:
    """Ключ для точной дедупликации, устойчивый к регистру и пунктуации."""
    normalized = re.sub(r"[^\wіїєґа-я]+", "", text.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def iter_source_posts(source_dir: Path):
    for html_path in sorted(source_dir.rglob("messages*.html")):
        channel = html_path.parent.name
        for timestamp, text in parse_html_file(html_path):
            yield timestamp, channel, text


def make_candidates(source_dir: Path, output_dir: Path, limit: int | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()

    for timestamp, channel, raw_text in iter_source_posts(source_dir):
        stats["total"] += 1
        result = classify_for_training(raw_text)
        if result.category == "irrelevant":
            stats[f"rejected:{result.reason}"] += 1
            rejected.append({"source": raw_text[:MAX_SOURCE_LENGTH], "channel": channel,
                             "timestamp": timestamp, "reason": result.reason})
            continue

        key = canonical_key(result.text)
        if key in seen:
            stats["rejected:exact_duplicate"] += 1
            continue
        seen.add(key)
        candidates.append({
            "id": key[:16],
            "input": result.text[:MAX_SOURCE_LENGTH],
            "candidate_type": result.category,
            "channel": channel,
            "timestamp": timestamp,
            "label_instruction": LABEL_PROMPT,
        })
        stats[f"candidate:{result.category}"] += 1
        if limit is not None and len(candidates) >= limit:
            break

    write_jsonl(output_dir / "candidates.jsonl", candidates)
    write_jsonl(output_dir / "rejected.jsonl", rejected)
    report = {"created_at": datetime.now().isoformat(timespec="seconds"), **stats,
              "candidates": len(candidates), "unique_texts": len(seen)}
    (output_dir / "candidate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Кандидаты: {len(candidates)}; отклонено: {len(rejected)}. Папка: {output_dir}")


def target_validation_reason(source: str, label: dict[str, Any]) -> str | None:
    """Не допустить очевидно опасные для качества LLM-ответы в обучение."""
    if label.get("keep") is not True:
        return "llm_rejected"
    if label.get("event_type") not in {"active_threat", "impact_or_shelling"}:
        return "invalid_event_type"
    target = str(label.get("target") or "").strip()
    if not target:
        return "empty_target"
    if len(target.split()) > MAX_TARGET_WORDS:
        return "target_too_long"
    if _URL_RE.search(target) or "@" in target or "#" in target:
        return "target_contains_promotion"
    forbidden_phrases = (
        "дяку", "залишайт", "залишайтеся", "укритт", "чекаємо", "чекайте",
        "можливо", "ймовірно", "вероятно", "будь ласка", "підпис",
    )
    if any(phrase in target.lower() for phrase in forbidden_phrases):
        return "target_contains_non_factual_text"
    if not target.endswith((".", "!", "…")):
        return "target_missing_terminal_punctuation"
    # Если в ответе указан Shahed/КАБ/ракета, исходник должен содержать хотя бы
    # соответствующий признак. Это не полная NLI-проверка, но ловит галлюцинации.
    lowered = source.lower()
    checks = {
        "shahed": ("шахед", "shahed", "герань", "геран"),
        "каб": ("каб", "kab"),
        "ракет": ("ракет", "баліст", "баллист", "калібр", "калибр", "кінжал", "кинжал"),
    }
    target_lowered = target.lower()
    for answer_token, source_tokens in checks.items():
        if answer_token in target_lowered and not any(token in lowered for token in source_tokens):
            return f"unsupported_weapon:{answer_token}"
    return None


def finalize_labels(labels_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    splits: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    needs_review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for line_number, line in enumerate(labels_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            needs_review.append({"line": line_number, "reason": "invalid_json", "raw": line[:1000]})
            continue
        source = str(record.get("input") or "").strip()
        reason = target_validation_reason(source, record)
        if reason:
            destination = needs_review if reason.startswith("unsupported_") else rejected
            destination.append({**record, "reason": reason})
            continue
        group = canonical_key(source)
        bucket = int(group[:8], 16) % 100
        split = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        splits[split].append({
            "input": source,
            "target": str(record["target"]).strip(),
            "event_type": record["event_type"],
            "channel": record.get("channel", ""),
            "timestamp": record.get("timestamp"),
            "dedup_group": group[:16],
        })

    for split, records in splits.items():
        write_jsonl(output_dir / f"{split}.jsonl", records)
    write_jsonl(output_dir / "needs_review.jsonl", needs_review)
    write_jsonl(output_dir / "rejected.jsonl", rejected)
    report = {"accepted": {name: len(rows) for name, rows in splits.items()},
              "needs_review": len(needs_review), "rejected": len(rejected)}
    (output_dir / "final_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Готово:", report)


def main() -> None:
    parser = argparse.ArgumentParser(description="Подготовка датасета AirRadar.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    candidates = subparsers.add_parser("candidates", help="Из HTML создать кандидаты для LLM-разметки.")
    candidates.add_argument("source", nargs="?", default=DEFAULT_SOURCE)
    candidates.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    candidates.add_argument("--limit", type=int, help="Остановиться после N кандидатов (пилот).")
    finalize = subparsers.add_parser("finalize", help="Проверить размеченный JSONL и разбить на наборы.")
    finalize.add_argument("labels", type=Path)
    finalize.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if args.command == "candidates":
        source = Path(args.source)
        if not source.is_dir():
            parser.error(f"Папка не найдена: {source}")
        make_candidates(source, args.output, args.limit)
    else:
        if not args.labels.is_file():
            parser.error(f"Файл не найден: {args.labels}")
        finalize_labels(args.labels, args.output)


if __name__ == "__main__":
    main()
