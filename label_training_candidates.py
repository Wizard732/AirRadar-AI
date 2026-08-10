#!/usr/bin/env python3
"""Разметить кандидаты через Ollama или Groq с возобновлением после остановки.

Пример (локальная Ollama):
  python label_training_candidates.py training_data/candidates.jsonl --backend ollama

Для Groq задай GROQ_API_KEY в окружении или .env:
  python label_training_candidates.py training_data/candidates.jsonl --backend groq
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import aiohttp

from prepare_training_dataset import LABEL_PROMPT


def response_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def valid_label(label: dict[str, Any] | None) -> bool:
    """Проверить контракт до записи: некорректный ответ надо перегенерировать."""
    if not label or not isinstance(label.get("keep"), bool):
        return False
    event_type = label.get("event_type")
    if event_type not in {"active_threat", "impact_or_shelling", "irrelevant"}:
        return False
    return isinstance(label.get("target"), str) and isinstance(label.get("reject_reason"), str)


async def label_one(session: aiohttp.ClientSession, backend: str, model: str, source: str) -> dict[str, Any] | None:
    """Разметить пост, повторив запрос до двух раз при нарушении JSON-контракта."""
    for _ in range(3):
        if backend == "ollama":
            payload = {"model": model, "system": LABEL_PROMPT, "prompt": source, "stream": False,
                       "format": "json", "options": {"temperature": 0.0, "num_predict": 160}}
            async with session.post("http://localhost:11434/api/generate", json=payload) as response:
                if response.status != 200:
                    continue
                label = response_json(str((await response.json()).get("response", "")))
        else:
            api_key = os.getenv("GROQ_API_KEY", "")
            if not api_key:
                raise RuntimeError("Для --backend groq задай GROQ_API_KEY.")
            payload = {"model": model, "temperature": 0.0, "max_tokens": 160, "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": LABEL_PROMPT}, {"role": "user", "content": source}]}
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers) as response:
                if response.status != 200:
                    continue
                data = await response.json()
                choices = data.get("choices") or []
                content = choices[0].get("message", {}).get("content", "") if choices else ""
                label = response_json(str(content))
        if valid_label(label):
            return label
    return None


async def run(args: argparse.Namespace) -> None:
    candidates = [json.loads(line) for line in args.candidates.read_text(encoding="utf-8").splitlines() if line.strip()]
    output = args.output
    done_ids: set[str] = set()
    if output.exists():
        done_ids = {json.loads(line).get("id", "") for line in output.read_text(encoding="utf-8").splitlines() if line.strip()}
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    written = 0
    errors = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with output.open("a", encoding="utf-8") as handle:
            for index, candidate in enumerate(candidates, 1):
                if candidate["id"] in done_ids:
                    continue
                label = await label_one(session, args.backend, args.model, candidate["input"])
                if label is None:
                    # Один плохой ответ не должен останавливать многочасовую
                    # разметку. Записываем его в отдельный файл для повтора.
                    errors += 1
                    with args.errors.open("a", encoding="utf-8") as errors_handle:
                        errors_handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")
                    print(f"[{index}/{len(candidates)}] Некорректный ответ, перенесён в {args.errors}.")
                    continue
                record = {key: candidate.get(key) for key in ("id", "input", "channel", "timestamp", "candidate_type")}
                record.update(label)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                written += 1
                if written % 25 == 0:
                    print(f"Размечено: {len(done_ids) + written}/{len(candidates)}")
                if args.delay:
                    await asyncio.sleep(args.delay)
    if errors:
        print(f"Некорректных ответов: {errors}; их можно перезапустить отдельным файлом.")
    print(f"Добавлено разметок: {written}. Файл: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-разметка кандидатов AirRadar.")
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--output", type=Path, default=Path("training_data/llm_labels.jsonl"))
    parser.add_argument(
        "--errors", type=Path,
        help="Кандидаты, на которых модель трижды не вернула корректный JSON (по умолчанию <output>.errors.jsonl).",
    )
    parser.add_argument("--backend", choices=("ollama", "groq"), default="ollama")
    parser.add_argument("--model", help="Модель разметчика; зависит от бэкенда.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--delay", type=float, default=0.0, help="Пауза между запросами для rate limit.")
    args = parser.parse_args()
    if not args.candidates.is_file():
        parser.error(f"Кандидаты не найдены: {args.candidates}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.errors is None:
        args.errors = args.output.with_suffix(".errors.jsonl")
    args.errors.parent.mkdir(parents=True, exist_ok=True)
    if not args.model:
        args.model = "qwen2.5:7b" if args.backend == "ollama" else "llama-3.1-8b-instant"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
