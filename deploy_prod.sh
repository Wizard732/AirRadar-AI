#!/usr/bin/env bash
# ============================================================
#  AirRadar AI — деплой з Mac на Proxmox CT 100 (по SSH-ключу, без паролів)
#
#    PROXMOX_HOST=<адреса хоста> bash deploy_prod.sh
#
#  Спершу ганяє тести; секрети (.env, *.session, *.db, ключі) в архів не потрапляють.
#  Потрібно: Tailscale увімкнений, ваш SSH-ключ у ~/.ssh/authorized_keys на хості.
# ============================================================
set -euo pipefail
HOST="${PROXMOX_HOST:?задайте PROXMOX_HOST}"
USER_="${PROXMOX_USER:-root}"
CT="${PROXMOX_CT:-100}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "[1/5] тести"
.venv/bin/python -m pytest -q -p no:cacheprovider
echo "[2/5] перевірка секретів"
.venv/bin/python secrets_check.py >/dev/null || echo "  (локально є ключі підпису — вони НЕ потраплять в архів)"
echo "[3/5] архів"
TAR="$(mktemp -t airradar).tgz"
COPYFILE_DISABLE=1 tar czf "$TAR" --exclude='.venv' --exclude='node_modules' --exclude='__pycache__' --exclude='.env' \
  --exclude='*.session*' --exclude='*.db*' --exclude='*.jks' --exclude='keystore.properties' \
  --exclude='models' --exclude='telegram_export' --exclude='.git' *.py *.sh *.service *.timer \
  *.md requirements.txt miniapp data map_live.html branding airradar tests_data 2>/dev/null || true
echo "[4/5] завантаження на $HOST"
scp -o BatchMode=yes "$TAR" "$USER_@$HOST:/tmp/airradar.tgz"
echo "[5/5] розгортання у CT $CT і перезапуск"
ssh -o BatchMode=yes "$USER_@$HOST" "pct push $CT /tmp/airradar.tgz /tmp/airradar.tgz && \
  pct exec $CT -- bash -c 'cd /opt/airradar && tar xzf /tmp/airradar.tgz && systemctl restart airradar-bot && sleep 3 && systemctl is-active airradar-bot'"
rm -f "$TAR"
echo "готово"
