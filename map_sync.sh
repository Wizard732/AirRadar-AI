#!/bin/bash
# map_sync.sh — регенерация карты и публикация снапшота на GitHub Pages.
set -e
cd /opt/airradar
./.venv/bin/python generate_map.py
git add map_live.html
if ! git diff --cached --quiet; then
  git commit -m "map: авто-снапшот угроз ($(date -u +'%Y-%m-%d %H:%M UTC'))"
  git push origin main
fi
