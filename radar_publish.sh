#!/bin/bash
# radar_publish.sh — публікує дані карти (tracks.json тощо) у гілку radar-data: один коміт, force-push.
# Mini App читає ці файли напряму з гілки (raw), тому свіжість = інтервал таймера, а не перезбирання Pages.
# Репозиторій створюється заново на кожен запуск: гілка — один коміт-сирота, історія не потрібна, а постійний
# .git на цьому хості вже тричі псувався після жорстких перезавантажень (порожні об'єкти) і зупиняв публікацію.
set -u
cd /opt/airradar
DIR=/opt/airradar-radar
mkdir -p "$DIR"
./.venv/bin/python export_radar.py "$DIR"
rc=$?
# 10 = «без змін»: публікуємо все одно не рідше ніж раз на 10 хв, щоб клієнт бачив, що дані живі
if [ "$rc" = 10 ]; then
  last=$(stat -c %Y "$DIR/.published" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -lt 600 ] && exit 0
elif [ "$rc" != 0 ]; then
  exit "$rc"
fi
cd "$DIR"
rm -rf .git
git init -q && git checkout -q --orphan radar-data
git remote add origin "$(git -C /opt/airradar remote get-url origin)"
git config user.name "AirRadar bot" && git config user.email "bot@airradar.local"
git add -A -- ':!.published'
git commit -q -m "radar data"
git push -q -f origin radar-data && touch "$DIR/.published"
