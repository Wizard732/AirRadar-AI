#!/usr/bin/env python3
"""generate_map.py — генератор автономной карты угроз.

Берёт активные угрозы из БД и вшивает их в map.html (данные внутри, без API).
Результат: самостоятельный HTML-файл, который можно открыть в браузере,
загрузить на Netlify или закрепить в канале. Обновление — перезапуск скрипта.

Запуск:
    python generate_map.py
Результат: map_live.html (готовая карта с данными).
"""

import json
import time
from pathlib import Path

from database import Database

TEMPLATE = Path(__file__).parent / "miniapp" / "map.html"
OUTPUT = Path(__file__).parent / "map_live.html"


def generate():
    db = Database("airradar.db")
    threats = db.active_threats(within_seconds=1800)
    now = int(time.time())
    data = [
        {
            "type": t["type"],
            "region": t["region"],
            "text": t["text"][:200],
            "age_min": (now - t["ts"]) // 60,
        }
        for t in threats
    ]
    db.close()

    # Читаем шаблон карты.
    html = TEMPLATE.read_text(encoding="utf-8")
    # Вставляем данные: заменяем fetch на встроенный массив.
    data_json = json.dumps(data, ensure_ascii=False)
    # Находим строку с API_URL и заменяем логику на встроенные данные.
    old_block = """// API URL — твой локальный сервер бота.
// ВАЖНО: замени на свой публичный IP/домен при деплое.
const API_URL = 'http://localhost:8080/api/threats';"""
    new_block = f"""// Данные встроены генератором (без API — автономная карта).
const API_URL = null;
const EMBEDDED_THREATS = {data_json};"""
    html = html.replace(old_block, new_block)

    # Заменяем функцию updateMap — берём встроенные данные вместо fetch.
    old_fetch = """  try {
    const resp = await fetch(API_URL);
    const data = await resp.json();
    const threats = data.threats || [];

    document.getElementById('counter').textContent = `${data.count || 0} активних загроз`;"""
    new_fetch = """  try {
    const threats = EMBEDDED_THREATS || [];

    document.getElementById('counter').textContent = `${threats.length} активних загроз`;"""
    html = html.replace(old_fetch, new_fetch)

    # Отключаем автообновление (нет API — обновление через перезапуск).
    html = html.replace("setInterval(updateMap, 30000);", "// Нет API — обновление через перезапуск generate_map.py")

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"✅ Карта сгенерирована: {OUTPUT}")
    print(f"   Угроз встроено: {len(data)}")
    print(f"   Открой в браузере: {OUTPUT.resolve()}")
    print(f"   Или загрузи на Netlify для публикации.")


if __name__ == "__main__":
    generate()
