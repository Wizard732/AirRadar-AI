#!/usr/bin/env bash
# ============================================================
#  AirRadar AI — обновление кода на VPS
#  Запускать при каждом обновлении (после git push с локалки):
#
#    sudo bash deploy.sh
#
#  Скрипт: подтягивает свежий код из GitHub, обновляет
#  зависимости (если requirements.txt менялся), перезапускает
#  сервис. .env и airradar.session НЕ трогаются.
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Цветные сообщения.
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }

info "Папка проекта: $PROJECT_DIR"

# --- 1. Сохраняем хеш до pull, чтобы понять, было ли обновление ---
BEFORE="$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || echo none)"

# --- 2. Тянем свежий код (секреты в .gitignore — не затрутся) ---
info "git pull…"
git -C "$PROJECT_DIR" pull --ff-only
AFTER="$(git -C "$PROJECT_DIR" rev-parse HEAD)"

# --- 3. Если код изменился — обновляем зависимости ---
if [[ "$BEFORE" != "$AFTER" ]]; then
  if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
    info "Обновляю зависимости (только если requirements.txt менялся)…"
    "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
  fi

  info "Перезапускаю сервис airradar…"
  systemctl restart airradar
  ok "Сервис перезапущен с новым кодом."
else
  ok "Код не изменился — перезапуск не нужен."
  warn "Хочешь перезапустить принудительно? sudo systemctl restart airradar"
fi

echo
info "Логи в реальном времени: sudo journalctl -u airradar -f"
info "Статус сервиса:          sudo systemctl status airradar"
