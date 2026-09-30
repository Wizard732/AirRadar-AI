"""health_server.py — HTTP-сервер: health-check + API для карты угроз.

Развитие: теперь не только /health, но и /api/threats — JSON со списком
активных угроз за последние 30 минут. Используется Mini App картой (Leaflet)
для отображения векторов движения и зон опасности в реальном времени.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Глобальная ссылка на Database — устанавливается из main.py через set_db.
# В aiohttp нельзя прокинуть аргумент в handler через route (без middleware),
# поэтому держим ссылку на уровне модуля.
_db = None
_app_db = None  # Database для API


def set_db(db) -> None:
    """Установить ссылку на Database (вызывается из main.py)."""
    global _app_db
    _app_db = db


async def _health(request: web.Request) -> web.Response:  # noqa: ANN001
    return web.json_response({"status": "ok", "service": "airradar"})


async def _api_threats(request: web.Request) -> web.Response:  # noqa: ANN001
    """Отдать активные угрозы для карты.

    ?minutes=N (1..120, по умолчанию 30) — глубина окна.
    Возвращает JSON: {window_min, count, threats: [{ts, type, region, text, age_min}]}
    """
    if _app_db is None:
        return web.json_response({"threats": [], "error": "db not ready"})
    try:
        try:
            minutes = int(request.query.get("minutes", "30")) if request is not None else 30
        except (ValueError, AttributeError):
            minutes = 30
        minutes = max(1, min(120, minutes))
        # Карта показывает и одиночные reported-события (полупрозрачно,
        # «очікує підтвердження») — иначе при 1 источнике карта пуста.
        # Стадия 'potential' («могут быть пуски») отсечена в БД: на карте
        # только то, что летит или уже попало, без домыслов.
        threats = _app_db.active_threats(within_seconds=minutes * 60, include_reported=True)
        # Репорты угроз от пользователей (share location): окно шире окна
        # угроз, но не больше 3 часов.
        report_window = min(180, max(30, minutes * 3))
        reports = _app_db.recent_geo_reports(report_window)
        # Статистика волн по затронутым регионам: «коли відбій» и
        # «коли наступна хвиля». Ошибки прогноза не роняют API.
        forecast: dict[str, dict] = {}
        try:
            from wave_forecast import region_forecast
            for slug in {t["region"] for t in threats}:
                fc = region_forecast(_app_db, slug)
                if fc:
                    forecast[slug] = fc
        except Exception as exc:  # noqa: BLE001
            logger.debug("forecast failed: %s", exc)
        # Добавим возраст в минутах. Текст — публичный (правило №6):
        # без ссылок/имён каналов и служебных префиксов, одна строка.
        from fast_filter import public_text
        now = int(time.time())
        result = []
        for t in threats:
            result.append({
                "ts": t["ts"],
                "type": t["type"],
                "region": t["region"],
                "text": public_text(t.get("text") or "", 200),
                "status": t.get("status", "corroborated"),
                "sources": t.get("source_count", 2),
                "origin": t.get("origin", ""),
                "destination": t.get("destination", ""),
                "age_min": max(0, (now - t["ts"]) // 60),
            })
        return web.json_response({
            "threats": result, "reports": reports, "forecast": forecast,
            "count": len(result), "window_min": minutes,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("API /api/threats error: %s", exc)
        return web.json_response({"threats": [], "error": "temporarily unavailable"}, status=503)


async def _api_stats(request: web.Request) -> web.Response:  # noqa: ANN001
    """Сводная статистика по типам за 24 часа."""
    if _app_db is None:
        return web.json_response({"error": "db not ready"})
    try:
        day = int(time.time()) - 86400
        counts = _app_db.threat_counts(region=None, since=day)
        return web.json_response({"counts": counts, "total": sum(counts.values())})
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)})


async def _serve_forever(port: int) -> None:
    """Бесконечная корутина: держит HTTP-сервер с health + API."""
    # CORS middleware — чтобы Mini App с Netlify мог делать запросы к API.
    @web.middleware
    async def cors(request: web.Request, handler):  # noqa: ANN001
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return resp

    app = web.Application(middlewares=[cors])
    app.router.add_get("/health", _health)
    app.router.add_get("/api/threats", _api_threats)
    app.router.add_get("/api/stats", _api_stats)
    map_path = Path(__file__).with_name("miniapp") / "map.html"
    map_handler = lambda request: web.FileResponse(map_path)  # noqa: E731
    # Корень и /map отдают карту: кнопка бота ведёт на корень URL туннеля
    # (MAP_WEBAPP_URL без пути), иначе пользователь видит health-JSON вместо
    # карты. Health-check живёт только на /health (его зовёт sync-скрипт).
    app.router.add_get("/", map_handler)
    app.router.add_get("/map", map_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP-сервер на порту %s (health + API /api/threats)", port)
    await asyncio.Event().wait()


async def start_health_server(port: int | None = None) -> asyncio.Task | None:
    if port is None:
        port = int(os.getenv("PORT", "8080"))
    try:
        return asyncio.create_task(_serve_forever(port), name="health-server")
    except Exception as exc:  # pragma: no cover
        logger.warning("Не удалось запустить health-сервер: %s", exc)
        return None
