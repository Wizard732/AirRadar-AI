#!/bin/bash
# map_sync.sh — регенерація автономної карти-снапшота. Більше НЕ комітить у main: файл іде у гілку radar-data
# разом з іншими даними (radar_publish.sh), а workflow бере його звідти. Так сервер не розходиться з main
# (раніше будь-який push з локалки ламав авто-снапшоти: «rejected, fetch first»).
set -e
cd /opt/airradar
./.venv/bin/python generate_map.py
mkdir -p /opt/airradar-radar
cp map_live.html /opt/airradar-radar/map_live.html
