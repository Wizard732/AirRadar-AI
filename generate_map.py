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
import sys
import time
from pathlib import Path

from database import Database

# Консоль Windows (cp1251) не умеет эмодзи из print — не роняем финальный вывод.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover
    pass

TEMPLATE = Path(__file__).parent / "miniapp" / "map.html"
OUTPUT = Path(__file__).parent / "map_live.html"


# Типы, которые действительно летят/бьют сейчас. Всё остальное
# («могут быть пуски», новости ВПК, болтовня) на карту не попадает:
# пользователь должен видеть цель, а не слухи.
MAP_THREAT_TYPES = frozenset({
    "shahed", "uav", "fpv", "recon_drone",
    "ballistic", "cruise_missile", "missile", "air_missile", "coastal_missile",
    "kab", "aviation", "tac_aviation", "strat_aviation",
    "mlrs", "artillery", "air_defense",
    "explosion",
    "other",   # тип уточняется, но инцидент реальный (stage=imminent/unknown)
})


def generate():
    db = Database("airradar.db")
    # Стаupia 'potential' («могут быть пуски») отсечены на уровне БД —
    # см. database.active_threats. Здесь второй рубеж: только летящие типы.
    threats = db.active_threats(within_seconds=1800, include_reported=True)
    now = int(time.time())
    data = [
        {
            "type": t["type"],
            "region": t["region"],
            "text": t["text"][:200],
            "sources": t.get("source_count", 2),
            "origin": t.get("origin", ""),
            "destination": t.get("destination", ""),
            "age_min": (now - t["ts"]) // 60,
        }
        for t in threats
        if t["type"] in MAP_THREAT_TYPES
    ]
    reports = [
        {"lat": r["lat"], "lon": r["lon"], "region": r["region"],
         "text": r["text"], "age_min": r["age_min"]}
        for r in db.recent_geo_reports(90)
    ]
    # Статистика волн по затронутым регионам («коли відбій / нова хвиля»).
    forecast = {}
    try:
        from wave_forecast import region_forecast
        for slug in {t["region"] for t in threats}:
            fc = region_forecast(db, slug, now=now)
            if fc:
                forecast[slug] = fc
    except Exception as exc:  # noqa: BLE001 — карта важнее прогноза
        print(f"⚠ Прогноз волн не построен: {exc}")
    db.close()

    # Читаем шаблон карты.
    html = TEMPLATE.read_text(encoding="utf-8")
    # Вставляем данные: заменяем fetch на встроенный массив.
    data_json = json.dumps(data, ensure_ascii=False)
    reports_json = json.dumps(reports, ensure_ascii=False)
    forecast_json = json.dumps(forecast, ensure_ascii=False)
    # Находим блок API_URL и заменяем логику на встроенные данные.
    # Анкеры должны совпадать с miniapp/map.html байт-в-байт.
    old_block = """// Same HTTPS origin as this Mini App; never use the user's localhost.
// Автономная версия (generate_map.py) заменяет этот блок на EMBEDDED_* данные.
const API_URL = '/api/threats';
const EMBEDDED_FORECAST = null;"""
    new_block = f"""// Данные встроены генератором (без API — автономная карта).
const API_URL = null;
const EMBEDDED_THREATS = {data_json};
const EMBEDDED_REPORTS = {reports_json};
const EMBEDDED_FORECAST = {forecast_json};"""
    html = html.replace(old_block, new_block)

    # Заменяем fetch в fetchData — берём встроенные данные вместо API.
    # Анкеры должны совпадать с miniapp/map.html байт-в-байт.
    old_fetch = """    const resp = await fetch(`${API_URL}?minutes=120`);
    const data = await resp.json();
    threatsCache = data.threats || [];
    reportsCache = data.reports || [];
    forecastCache = data.forecast || EMBEDDED_FORECAST || {};"""
    new_fetch = """    threatsCache = EMBEDDED_THREATS || [];
    reportsCache = EMBEDDED_REPORTS || [];
    forecastCache = EMBEDDED_FORECAST || {};"""
    html = html.replace(old_fetch, new_fetch)

    # Отключаем автообновление (нет API — обновление через перезапуск).
    html = html.replace(
        "setInterval(fetchData, 15000);",
        "// Нет API — обновление через перезапуск generate_map.py",
    )

    if "EMBEDDED_THREATS" not in html or "EMBEDDED_FORECAST" not in html:
        raise SystemExit(
            "❌ Анкеры шаблона miniapp/map.html не совпали — карта НЕ обновлена. "
            "Проверь API_URL-блок и fetchData в шаблоне."
        )

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"✅ Карта сгенерирована: {OUTPUT}")
    print(f"   Угроз встроено: {len(data)}, регионов с прогнозом: {len(forecast)}")
    print(f"   Открой в браузере: {OUTPUT.resolve()}")
    print(f"   Или загрузи на Netlify для публикации.")


if __name__ == "__main__":
    generate()
