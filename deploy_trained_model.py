#!/usr/bin/env python3
"""Слить QLoRA-адаптер, проверить модель и безопасно переключить Ollama-бот."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_ADAPTER = Path("models/airradar-qwen7b")
DEFAULT_MERGED = Path("models/airradar-qwen7b-merged")
DEFAULT_OLLAMA_MODEL = "airradar-qwen7b"


def ollama_generate(model: str, prompt: str) -> str:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.0, "num_predict": 80}}).encode()
    request = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return str(json.loads(response.read()).get("response", "")).strip()


def verify_model(model: str) -> None:
    """Проверить ключевые свойства до смены production-модели."""
    checks = (
        ("Шах курсом на Суми", ("shahed", "бпла")),
        ("КАБ курсом на Суми", ("каб",)),
        ("У Києві чути звук, схожий на мопед", ("shahed", "бпла")),
    )
    for prompt, forbidden_or_expected in checks:
        answer = ollama_generate(model, prompt).lower()
        if not answer:
            raise RuntimeError(f"Модель не вернула ответ на: {prompt}")
        if "мопед" in prompt.lower():
            if any(token in answer for token in forbidden_or_expected):
                raise RuntimeError(f"Небезопасная идентификация наблюдения: {answer}")
        elif not any(token in answer for token in forbidden_or_expected):
            raise RuntimeError(f"Модель потеряла явный тип оружия: {answer}")


def switch_env_model(env_path: Path, model: str) -> str:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    old = next((line.partition("=")[2] for line in lines if line.startswith("OLLAMA_MODEL=")), "")
    updated = [f"OLLAMA_MODEL={model}" if line.startswith("OLLAMA_MODEL=") else line for line in lines]
    if not any(line.startswith("OLLAMA_MODEL=") for line in updated):
        updated.append(f"OLLAMA_MODEL={model}")
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return old


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--merged", type=Path, default=DEFAULT_MERGED)
    parser.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    parser.add_argument("--service", default="airradar-bot.service")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()

    if not (args.adapter / "adapter_config.json").is_file():
        raise SystemExit(f"Адаптер не найден: {args.adapter}")
    if not args.env.is_file():
        raise SystemExit(f"Файл настроек не найден: {args.env}")

    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch

    args.merged.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model, torch_dtype=torch.float16, device_map="cpu", trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(model, args.adapter).merge_and_unload()
    model.save_pretrained(args.merged, safe_serialization=True)
    tokenizer.save_pretrained(args.merged)

    modelfile = Path("models/Modelfile.airradar")
    modelfile.write_text(
        f"FROM {args.merged.resolve()}\nPARAMETER temperature 0\nPARAMETER num_predict 80\n",
        encoding="utf-8",
    )
    subprocess.run(["ollama", "create", args.ollama_model, "-f", str(modelfile)], check=True)
    verify_model(args.ollama_model)

    previous_model = switch_env_model(args.env, args.ollama_model)
    try:
        subprocess.run(["systemctl", "restart", args.service], check=True)
    except subprocess.CalledProcessError:
        switch_env_model(args.env, previous_model)
        subprocess.run(["systemctl", "restart", args.service], check=False)
        raise
    print(f"Готово: бот переключён на {args.ollama_model}; предыдущая модель: {previous_model or 'не задана'}.")


if __name__ == "__main__":
    main()
