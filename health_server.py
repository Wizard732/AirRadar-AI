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
        threats = _app_db.active_threats(within_seconds=minutes * 60, include_reported=True)
        # Добавим возраст в минутах.
        now = int(time.time())
        result = []
        for t in threats:
            result.append({
                "ts": t["ts"],
                "type": t["type"],
                "region": t["region"],
                "text": t["text"][:200],
                "status": t.get("status", "corroborated"),
                "sources": t.get("source_count", 2),
                "age_min": max(0, (now - t["ts"]) // 60),
            })
        return web.json_response({"threats": result, "count": len(result), "window_min": minutes})
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
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/api/threats", _api_threats)
    app.router.add_get("/api/stats", _api_stats)
    map_path = Path(__file__).with_name("miniapp") / "map.html"
    app.router.add_get("/map", lambda request: web.FileResponse(map_path))
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
