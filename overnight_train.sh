#!/usr/bin/env bash
# Автозапуск: дождаться импорта истории, проверить обучение и запустить QLoRA.
# Работает на одной GPU (GPU 0) и пишет полный лог в training_data/training.log.
set -Eeuo pipefail

cd /opt/airradar
# Результаты обучения живут на хостовом диске: они переживают рестарт CT 100.
MODEL_ROOT=/mnt/airradar-models/training
mkdir -p training_data "$MODEL_ROOT"
LOG=training_data/overnight_train.log
exec >>"$LOG" 2>&1

echo "[$(date -Is)] Autotrain started"
while pgrep -f '[i]mport_history.py' >/dev/null; do
  echo "[$(date -Is)] Waiting for historical import..."
  sleep 60
done

echo "[$(date -Is)] Historical import finished; verifying dataset"
test -s training_data/silver/train.jsonl
test -s training_data/silver/validation.jsonl
test -s training_data/silver/test.jsonl

# Не конкурировать с ботом за GPU во время обучения.
systemctl stop airradar-bot.service 2>/dev/null || true
export CUDA_VISIBLE_DEVICES=0,1
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1

# Smoke-run даёт раннюю ошибку окружения и не начинает полный train вслепую.
python="/opt/airradar/.venv-train/bin/python"
# Smoke-test нужен только перед первым запуском. При рестарте сохраняем прогресс.
ADAPTER="$MODEL_ROOT/airradar-qwen7b-ddp2"
LAUNCH=("$python" -m accelerate.commands.launch --num_processes 2 --num_machines 1 --mixed_precision no)
if ! find "$ADAPTER" -maxdepth 1 -type d -name 'checkpoint-*' | grep -q . && [ ! -f "$ADAPTER/adapter_model.safetensors" ]; then
  rm -rf training_data/smoke "$MODEL_ROOT/smoke-ddp2"
  mkdir -p training_data/smoke
  head -n 10 training_data/silver/train.jsonl > training_data/smoke/train.jsonl
  head -n 2 training_data/silver/validation.jsonl > training_data/smoke/validation.jsonl
  "${LAUNCH[@]}" train_qlora.py training_data/smoke --output "$MODEL_ROOT/smoke-ddp2" --smoke
  echo "[$(date -Is)] Smoke training succeeded"
fi

echo "[$(date -Is)] Starting/resuming full QLoRA on durable storage"
"${LAUNCH[@]}" train_qlora.py training_data/silver --output "$ADAPTER" --epochs 1 --resume

echo "[$(date -Is)] Full QLoRA succeeded"
touch "$ADAPTER/TRAINING_COMPLETE"
# Активация запускается отдельно после проверки адаптера; не удаляет результат.
