#!/usr/bin/env bash
# ============================================================
#  AirRadar AI — первичная установка на VPS (Ubuntu/Debian ARM)
#  Запускать ОДИН раз на свежем сервере:
#
#    sudo bash install.sh
#
#  Скрипт: ставит системные пакеты, Ollama + модель, виртуальное
#  окружение Python, зависимости проекта, копирует systemd-юнит.
#  НЕ запускает сервис — для запуска см. DEPLOY.md (нужен .env и
#  интерактивный вход в Telegram).
# ============================================================
set -euo pipefail

# Сообщения с префиксом для наглядности.
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()   { echo -e "\033[1;31m[ERR]\033[0m   $*" >&2; }

# --- проверка рута ---
if [[ $EUID -ne 0 ]]; then
  err "Запусти через sudo: sudo bash install.sh"
  exit 1
fi

# Рабочая папка проекта — там же, откуда запущен скрипт.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-root}"
info "Папка проекта: $PROJECT_DIR"
info "Пользователь сервиса: $SERVICE_USER"

# ------------------------------------------------------------
# 1. Системные пакеты
# ------------------------------------------------------------
info "Устанавливаю системные пакеты (python3, pip, venv, git, curl)…"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl ca-certificates > /dev/null
ok "Системные пакеты готовы"

# ------------------------------------------------------------
# 2. Ollama + модель qwen2.5:3b
# ------------------------------------------------------------
if ! command -v ollama &>/dev/null; then
  info "Устанавливаю Ollama…"
  curl -fsSL https://ollama.com/install.sh | sh
  ok "Ollama установлена"
else
  ok "Ollama уже установлена"
fi

info "Включаю автозапуск Ollama как сервис…"
systemctl enable --now ollama || warn "Сервис ollama уже активен"

info "Скачиваю модель qwen2.5:3b (≈2 ГБ, может занять время)…"
sudo -u "$SERVICE_USER" ollama pull qwen2.5:3b
ok "Модель готова"

# ------------------------------------------------------------
# 3. Виртуальное окружение Python + зависимости
# ------------------------------------------------------------
info "Создаю виртуальное окружение .venv…"
sudo -u "$SERVICE_USER" python3 -m venv "$PROJECT_DIR/.venv"

info "Ставлю зависимости проекта…"
# На Debian нужен актуальный pip + отключение PEP 668 для venv-установки.
sudo -u "$SERVICE_USER" "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$SERVICE_USER" "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
ok "Зависимости установлены"

# ------------------------------------------------------------
# 4. systemd-юнит — автозапуск и авто-рестарт бота
# ------------------------------------------------------------
info "Устанавливаю systemd-юнит airradar…"
UNIT_PATH="/etc/systemd/system/airradar.service"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=AirRadar AI — Telegram threat monitor
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
#ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/main.py
ExecStart=$PROJECT_DIR/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
# Логи уходят в journalctl — смотреть: journalctl -u airradar -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
ok "Юнит установлен: $UNIT_PATH"

systemctl daemon-reload
ok "Установка завершена."

echo
warn "Дальнейшие шаги (см. DEPLOY.md):"
echo "  1) Создай $PROJECT_DIR/.env из .env.example и заполни секреты."
echo "  2) Первый запуск интерактивно (для входа в Telegram):"
echo "       sudo -u $SERVICE_USER $PROJECT_DIR/.venv/bin/python main.py"
echo "     Введи номер телефона и код — создастся airradar.session."
echo "     Затем нажми Ctrl+C."
echo "  3) Запусти как сервис с авто-рестартом:"
echo "       sudo systemctl enable --now airradar"
echo "  4) Смотри логи в реальном времени:"
echo "       sudo journalctl -u airradar -f"
