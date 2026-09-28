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

# Идемпотентность: если полный програн уже завершён и адаптер собран —
# выходим сразу (юнит остаётся enabled на случай рестартов контейнера).
ADAPTER="$MODEL_ROOT/airradar-qwen7b-full"
if [ -f "$ADAPTER/TRAINING_COMPLETE" ] && [ -f "$ADAPTER/adapter_model.safetensors" ]; then
  echo "[$(date -Is)] TRAINING_COMPLETE уже есть — обучение не требуется"
  exit 0
fi

# Не конкурировать с ботом за GPU во время обучения: бот и Ollama живут на
# GPU 0 (Ollama закреплена за GPU 0 в своём окружении), обучаемся на GPU 1.
# Бота НЕ останавливаем: он не использует GPU напрямую, а простой ломает
# правило «отбой публикуется всегда и мгновенно».
export CUDA_VISIBLE_DEVICES=1
export TOKENIZERS_PARALLELISM=false
export PYTHONUNBUFFERED=1

# Ранняя ошибка окружения вместо слепого старта полного train:
# torch обязан видеть CUDA (P100), иначе падать лучше сразу.
python_check="/opt/airradar/.venv-train/bin/python"
if ! "$python_check" -c 'import sys; assert sys.version_info >= (3, 10); import torch; assert torch.cuda.is_available(), "CUDA недоступна (CPU-torch?)"' >/dev/null 2>&1; then
  echo "[$(date -Is)] FATAL: .venv-train torch без CUDA — обучение отменено"
  exit 1
fi

# Smoke-run даёт раннюю ошибку окружения и не начинает полный train вслепую.
python="/opt/airradar/.venv-train/bin/python"
# Smoke-test нужен только перед первым запуском. При рестарте сохраняем прогресс.
if ! find "$ADAPTER" -maxdepth 1 -type d -name 'checkpoint-*' | grep -q . && [ ! -f "$ADAPTER/adapter_model.safetensors" ]; then
  rm -rf training_data/smoke "$MODEL_ROOT/smoke"
  mkdir -p training_data/smoke
  head -n 10 training_data/silver/train.jsonl > training_data/smoke/train.jsonl
  head -n 2 training_data/silver/validation.jsonl > training_data/smoke/validation.jsonl
  "$python" train_qlora.py training_data/smoke --output "$MODEL_ROOT/smoke" --smoke
  echo "[$(date -Is)] Smoke training succeeded"
fi

echo "[$(date -Is)] Starting/resuming full QLoRA on durable storage"
"$python" train_qlora.py training_data/silver --output "$ADAPTER" --epochs 1 --resume

echo "[$(date -Is)] Full QLoRA succeeded"
touch "$ADAPTER/TRAINING_COMPLETE"
# Активация запускается отдельно после проверки адаптера; не удаляет результат.
