#!/usr/bin/env python3
"""Собрать финальную Ollama-модель из QLoRA-адаптера на большом диске сервера."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--name", default="airradar-qwen7b")
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct")
    args = parser.parse_args()
    if not (args.adapter / "adapter_model.safetensors").is_file():
        parser.error(f"Адаптер не найден: {args.adapter}")

    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch

    args.merged.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(args.base_model, torch_dtype=torch.float16, device_map="cpu", trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.adapter).merge_and_unload()
    model.save_pretrained(args.merged, safe_serialization=True)
    tokenizer.save_pretrained(args.merged)
    modelfile = args.merged.parent / "Modelfile.airradar"
    modelfile.write_text(f"FROM {args.merged.resolve()}\nPARAMETER temperature 0\nPARAMETER num_predict 80\n", encoding="utf-8")
    subprocess.run(["ollama", "create", args.name, "-f", str(modelfile)], check=True)


if __name__ == "__main__":
    main()
