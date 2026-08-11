#!/usr/bin/env bash
# Автозапуск: дождаться импорта истории, проверить обучение и запустить QLoRA.
# Работает на одной GPU (GPU 0) и пишет полный лог в training_data/training.log.
set -Eeuo pipefail

cd /opt/airradar
mkdir -p training_data models
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
export CUDA_VISIBLE_DEVICES=0
export PYTHONUNBUFFERED=1

# Smoke-run даёт раннюю ошибку окружения и не начинает полный train вслепую.
python="/opt/airradar/.venv-train/bin/python"
# Smoke-test нужен только перед первым запуском. При рестарте сохраняем прогресс.
if ! find models/airradar-qwen7b -maxdepth 1 -type d -name 'checkpoint-*' | grep -q .; then
  rm -rf training_data/smoke models/smoke
  mkdir -p training_data/smoke
  head -n 10 training_data/silver/train.jsonl > training_data/smoke/train.jsonl
  head -n 2 training_data/silver/validation.jsonl > training_data/smoke/validation.jsonl
  "$python" train_qlora.py training_data/smoke --output models/smoke --smoke
  echo "[$(date -Is)] Smoke training succeeded"
fi

echo "[$(date -Is)] Starting/resuming full QLoRA"
"$python" train_qlora.py training_data/silver --output models/airradar-qwen7b --max-samples 4000 --epochs 1 --resume

echo "[$(date -Is)] Full QLoRA succeeded"
touch models/airradar-qwen7b/TRAINING_COMPLETE
# Сливаем адаптер, импортируем в Ollama и сразу переключаем бота на новую модель.
"$python" deploy_trained_model.py
echo "[$(date -Is)] Bot switched to the trained model"
