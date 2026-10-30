#!/bin/bash
# radar_publish.sh — публикует tracks.json в ветку radar-data (один коммит, force-push).
# Ветку подхватывает workflow deploy-map.yml и выкладывает на GitHub Pages.
set -u
cd /opt/airradar
DIR=/opt/airradar-radar
if [ ! -d "$DIR/.git" ]; then
  mkdir -p "$DIR" && cd "$DIR"
  git init -q && git checkout -q --orphan radar-data
  git remote add origin "$(git -C /opt/airradar remote get-url origin)"
  git config user.name "AirRadar bot" && git config user.email "bot@airradar.local"
  cd /opt/airradar
fi
./.venv/bin/python export_radar.py "$DIR"
rc=$?
[ "$rc" = 10 ] && exit 0
[ "$rc" != 0 ] && exit "$rc"
cd "$DIR"
git add tracks.json stats.json impacts.json alerts_hist.json
git commit -q --amend -m "radar data" 2>/dev/null || git commit -q -m "radar data"
git push -q -f origin radar-data
