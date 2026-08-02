"""health_server.py — мини HTTP-сервер для PaaS-платформ (Koyeb и др.).

Зачем: Koyeb/Render/Fly ждут, что приложение открывает HTTP-порт и отвечает
на запросы — по этому порту платформа понимает, что сервис жив («health check»).
Сам бот — фоновый слушатель Telethon, без веба. Этот модуль поднимает рядом
крошечный HTTP-сервер на порту из переменной окружения PORT (или 8080), который
отвечает 200 OK на любой запрос. Логику бота он не трогает.
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)


async def _health(request: web.Request) -> web.Response:  # noqa: ANN001
    """Эндпоинт health-check: всегда 200 OK с JSON-статусом."""
    return web.json_response({"status": "ok", "service": "airradar"})


async def _serve_forever(port: int) -> None:
    """Бесконечная корутина: держит HTTP-сервер живым всё время работы бота."""
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-сервер слушает порт %s (для health-check платформы)", port)
    # Бесконечно висим, пока бот работает. Выход — через cancel задачи.
    await asyncio.Event().wait()


async def start_health_server(port: int | None = None) -> asyncio.Task | None:
    """Запустить HTTP-сервер health-check в фоне, вернуть task (или None при ошибке).

    port: если None — берётся из переменной окружения PORT (стандарт PaaS),
          иначе 8080. Функция не падает при ошибке (порт занят и т.п.) —
          логирует warning, потому что бот должен работать даже без health-сервера.
    """
    if port is None:
        port = int(os.getenv("PORT", "8080"))
    try:
        return asyncio.create_task(_serve_forever(port), name="health-server")
    except Exception as exc:  # pragma: no cover
        logger.warning("Не удалось запустить health-сервер: %s", exc)
        return None
