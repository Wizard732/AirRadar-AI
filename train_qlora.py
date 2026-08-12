#!/usr/bin/env python3
"""QLoRA дообучение Qwen2.5-7B-Instruct на AirRadar JSONL.

Запуск на Linux/P100:
  python train_qlora.py training_data/silver --output models/airradar-qwen7b
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-samples", type=int, default=0, help="Ограничить train-набор; 0 = весь train.jsonl.")
    parser.add_argument("--resume", action="store_true", help="Продолжить с последнего checkpoint в --output.")
    parser.add_argument("--smoke", action="store_true", help="10 строк / 1 step для проверки окружения.")
    args = parser.parse_args()

    try:
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import SFTConfig, SFTTrainer
        import torch
    except ImportError as exc:
        raise SystemExit("Установи зависимости: pip install -r requirements-train.txt") from exc

    files = {"train": str(args.dataset / "train.jsonl"), "validation": str(args.dataset / "validation.jsonl")}
    dataset = load_dataset("json", data_files=files)
    if args.smoke:
        dataset["train"] = dataset["train"].select(range(min(10, len(dataset["train"]))))
    elif args.max_samples:
        dataset["train"] = dataset["train"].select(range(min(args.max_samples, len(dataset["train"]))))
    system = "Ти військовий редактор. Поверни одне коротке українське повідомлення лише з фактами джерела. Не вигадуй деталей."

    def format_example(row: dict) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": row["input"]},
                    {"role": "assistant", "content": row["target"]}]
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.float16)
    # Одна P100: самый стабильный режим для текущего TRL/PyTorch стека.
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=torch.float16,
    )
    model.gradient_checkpointing_disable()
    model.config.use_cache = True
    # P100 не поддерживает BF16. 4-bit вычисления модели остаются FP16, но
    # Trainer запускаем без AMP scaler: свежий accelerate иначе пытается
    # unscale BF16-градиенты Qwen и аварийно завершается.
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM", target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    config = SFTConfig(output_dir=str(args.output), num_train_epochs=args.epochs, learning_rate=1e-4,
                       per_device_train_batch_size=2, per_device_eval_batch_size=1, gradient_accumulation_steps=8,
                       fp16=False, bf16=False, max_grad_norm=0.0, max_length=192, logging_steps=25,
                       eval_strategy="no", save_strategy="steps", save_steps=50, save_total_limit=3,
                       report_to="none")
    trainer = SFTTrainer(model=model, args=config, train_dataset=dataset["train"], eval_dataset=dataset["validation"],
                         processing_class=tokenizer, peft_config=lora, formatting_func=format_example)
    checkpoint = None
    if args.resume:
        checkpoints = sorted(args.output.glob("checkpoint-*"), key=lambda path: int(path.name.split("-")[-1]))
        checkpoint = str(checkpoints[-1]) if checkpoints else None
    trainer.train(resume_from_checkpoint=checkpoint)
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))


if __name__ == "__main__":
    main()
