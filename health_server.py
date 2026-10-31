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

from airradar.safety import public_view as _pv

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


_LEGACY_FOR_TESTS = False   # тести внутрішнього формату


def public_mode() -> str:
    """legacy (за замовчуванням, рішення власника) — повні дані карти, як раніше: координати, швидкість, курс, прогноз.
    region — узагальнене подання за районами (PUBLIC_MODE=region), реалізоване в airradar/safety/public_view.py."""
    if _LEGACY_FOR_TESTS:
        return "legacy"
    return "region" if os.getenv("PUBLIC_MODE", "").strip().lower() == "region" else "legacy"


def public_delay_s() -> int:
    """Затримка публічних даних: TRACKS_PUBLIC_DELAY_MIN, якщо задано; інакше 10 хв у режимі region і 0 у legacy."""
    raw = os.getenv("TRACKS_PUBLIC_DELAY_MIN")
    if raw is not None and raw.strip() != "":
        try:
            return max(0, int(raw)) * 60
        except ValueError:
            pass
    return _pv.DEFAULT_DELAY_MIN * 60 if public_mode() == "region" else 0


def public_payload(db, minutes: int, now: int) -> dict:
    """Публічне подання радара. `now` — реальний час; затримка застосовується тут. У legacy — повний payload."""
    delay = public_delay_s()
    private = radar_payload(db, minutes, now - delay)
    if public_mode() == "legacy":
        return private
    return _pv.build_public(private, now - delay, delay // 60)


_risk_hits: dict[str, list[float]] = {}
_RISK_LIMIT, _RISK_WINDOW_S = 20, 60


def _risk_allowed(request) -> bool:  # noqa: ANN001
    """Простий ліміт запитів на IP для /api/risk (захист від підбору позиції серією запитів)."""
    ip = (getattr(request, "headers", {}) or {}).get("X-Forwarded-For", "") or str(getattr(request, "remote", "") or "")
    ip = ip.split(",")[0].strip()
    now = time.time()
    hits = [t for t in _risk_hits.get(ip, []) if now - t < _RISK_WINDOW_S]
    if len(hits) >= _RISK_LIMIT:
        _risk_hits[ip] = hits
        return False
    hits.append(now)
    _risk_hits[ip] = hits
    if len(_risk_hits) > 5000:
        _risk_hits.clear()
    return True


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
        # Вектор скорости по засечкам (track_speed): курс/скорость/ETA для
        # карты. Ошибки трека не роняют API — просто отдаём track: null.
        from track_speed import build_track, track_payload
        from weapon_classes import detect_speed_profile
        now = int(time.time())
        result = []
        for t in threats:
            try:
                track = track_payload(build_track(
                    _app_db, t["region"], t["type"], t["ts"],
                    speed_profile=detect_speed_profile(t.get("text") or "", t["type"]),
                ))
            except Exception as exc:  # noqa: BLE001 — карта важнее трека
                logger.debug("track build failed for %s: %s", t["region"], exc)
                track = None
            result.append({
                "ts": t["ts"],
                "type": t["type"],
                "region": t["region"],
                "text": public_text(t.get("text") or "", 200),
                "status": t.get("status", "corroborated"),
                "sources": t.get("source_count", 2),
                "origin": t.get("origin", ""),
                "destination": t.get("destination", ""),
                "track": track,
                "age_min": max(0, (now - t["ts"]) // 60),
            })
        if public_mode() == "region":
            # Публічно без вектора руху (курс, швидкість, from/to) і напрямку; точки репортів — до центроїда району.
            result = [_pv.strip_threat(x) for x in result]
            reports = [r for r in (_pv.coarsen_report(x) for x in reports) if r]
        return web.json_response({
            "threats": result, "reports": reports, "forecast": forecast,
            "count": len(result), "window_min": minutes,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("API /api/threats error: %s", exc)
        return web.json_response({"threats": [], "error": "temporarily unavailable"}, status=503)


def _track_json(t, now: int, official: set | None = None) -> dict:
    from track_engine import _is_official, compass_ua, forecast_places, position_at
    lat, lon, est = position_at(t, now)
    return {
        "sources": max(1, len(t.sources)), "official": _is_official(lat, lon, official),
        "id": t.key, "weapon": t.weapon, "profile": t.profile, "status": t.status,
        "count": t.count,
        "speed_kmh": t.speed_kmh, "speed_source": t.speed_source,
        "heading": None if t.heading is None else round(t.heading),
        "heading_text": "" if t.heading is None else compass_ua(t.heading),
        "now": {"lat": round(lat, 4), "lon": round(lon, 4), "estimated": est},
        "points": [{"ts": p.ts, "lat": round(p.lat, 4), "lon": round(p.lon, 4), "count": p.count,
                    "place": p.place, "precision": p.precision} for p in t.points],
        "forecast": [{"name": p.name, "lat": p.lat, "lon": p.lon, "eta_min": p.eta_min, "km": p.km}
                     for p in forecast_places(t, now)],
        "dest": None if t.dest is None else {"lat": round(t.dest[0], 4), "lon": round(t.dest[1], 4), "name": t.dest[2]},
    }


def radar_payload(db, minutes: int, now: int) -> dict:
    """Единый JSON для /api/tracks и статического tracks.json: треки + одиночные события."""
    from track_engine import build_events, build_tracks
    rows = db.recent_observations(now - minutes * 60)
    tracks = build_tracks(rows, now, window_s=minutes * 60)
    off_set = set(db.siren_states_fresh()) if os.getenv("ALERTS_IN_UA_TOKEN", "").strip() else None
    impacts = []
    try:
        import impact_log
        impacts = [i for i in impact_log.impacts_payload(db, now, 0, days=1)["items"] if i["lat"] is not None
                   and now - i["ts"] <= 6 * 3600]
    except Exception:  # noqa: BLE001 — наслідки не должны ломать карту
        pass
    # Офіційні сирени (alerts.in.ua): None — джерело не підключене, тоді карта показує область за зведеннями.
    on = bool(os.getenv("ALERTS_IN_UA_TOKEN", "").strip())
    fresh = db.siren_states_fresh() if on else []
    sirens = sorted(k for k in fresh if not k.startswith(("raion", "threat:"))) if on else None
    sirens_raions = sorted(k[6:] for k in fresh if k.startswith("raion:")) if on else None
    sirens_raions_y = sorted(k[7:] for k in fresh if k.startswith("raionY:")) if on else None
    sirens_raions_p = sorted(k[7:] for k in fresh if k.startswith("raionP:")) if on else None
    detail: list = []
    if on:
        try:
            import json as _json
            from config import load_settings
            _d = _json.loads((Path(load_settings().db_path).resolve().parent / "siren_detail.json").read_text(encoding="utf-8"))
            if now - int(_d.get("ts", 0)) <= 600:
                detail = _d.get("alerts", [])
        except Exception:  # noqa: BLE001
            detail = []
    threats: dict[str, list[str]] = {}
    for k in fresh if on else []:
        if k.startswith("threat:"):
            _, slug, kind = k.split(":", 2)
            threats.setdefault(slug, []).append(kind)
    try:
        import forecast_eval
        accuracy = forecast_eval.accuracy_regions(db, now, 30)
    except Exception:  # noqa: BLE001
        accuracy = None
    feed: list = []
    try:   # стрічка «Загрози»: свіжі інциденти зі зведень каналів (без назв джерел — правило №6)
        from fast_filter import public_text
        feed = [{"ts": t["ts"], "text": public_text(t.get("text") or "", 220), "region": t["region"], "type": t["type"]}
                for t in db.active_threats(within_seconds=3 * 3600, include_reported=True)][:60]
    except Exception:  # noqa: BLE001
        pass
    return {"now": now, "window_min": minutes, "tracks": [_track_json(t, now, off_set) for t in tracks],
            "events": build_events(rows, now), "impacts": impacts, "sirens": sirens, "sirens_raions": sirens_raions, "sirens_raions_y": sirens_raions_y,
            "sirens_raions_p": sirens_raions_p, "siren_threats": threats if on else None, "alerts": detail, "feed": feed, "accuracy": accuracy}


def history_payload(db, now: int, days: int = 7) -> dict:
    """Журнал офіційних тривог за N днів: alerts=[id,slug,type,title,raion,level,kind,start,end], events=[ts,slug,title,raion,kind,detail]."""
    out = db.alert_history(now - days * 86400)
    out["now"] = now
    return out


async def _api_alerts_history(request: web.Request) -> web.Response:  # noqa: ANN001
    if _app_db is None:
        return web.json_response({"alerts": [], "events": [], "error": "db not ready"})
    try:
        days = max(1, min(30, int(request.query.get("days", "7"))))
    except ValueError:
        days = 7
    return web.json_response(history_payload(_app_db, int(time.time()), days))


async def _api_tracks(request: web.Request) -> web.Response:  # noqa: ANN001
    """Треки целей: ?minutes=N (по умолчанию 180, до 720). Позиции — для воспроизведения во времени."""
    if _app_db is None:
        return web.json_response({"tracks": [], "error": "db not ready"})
    try:
        minutes = max(10, min(720, int(request.query.get("minutes", "180"))))
    except ValueError:
        minutes = 180
    try:
        # Публічне подання (район, без live-координат) із тією самою затримкою, що й для статичного tracks.json.
        return web.json_response(public_payload(_app_db, minutes, int(time.time())))
    except Exception as exc:  # noqa: BLE001
        logger.warning("API /api/tracks error: %s", exc)
        return web.json_response({"tracks": [], "error": "temporarily unavailable"}, status=503)


async def _api_public_regions(request: web.Request) -> web.Response:  # noqa: ANN001
    """Чистий публічний контракт v1: райони/області, категорії, статуси, діапазони. Без координат."""
    if _app_db is None:
        return web.json_response({"regions": [], "error": "db not ready"})
    try:
        minutes = max(10, min(720, int(request.query.get("minutes", "180"))))
    except ValueError:
        minutes = 180
    try:
        delay = public_delay_s()
        private = radar_payload(_app_db, minutes, int(time.time()) - delay)
        return web.json_response(_pv.regions_contract(_pv.build_public(private, int(time.time()) - delay, delay // 60)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("API /api/public/v1/regions error: %s", exc)
        return web.json_response({"regions": [], "error": "temporarily unavailable"}, status=503)


async def _admin_tracks(request: web.Request) -> web.Response:  # noqa: ANN001
    """Повний (закритий) payload треків — лише з X-Admin-Token; без токена в оточенні — 404."""
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    if _app_db is None:
        return web.json_response({"tracks": [], "error": "db not ready"}, status=503)
    try:
        minutes = max(10, min(720, int(request.query.get("minutes", "180"))))
    except ValueError:
        minutes = 180
    return web.json_response(radar_payload(_app_db, minutes, int(time.time())))


_sse_clients = [0]
_SSE_MAX = 200
_stream_rev = {"rev": "", "ts": 0.0}


def _compute_rev() -> str:
    """Короткий відбиток публічного стану (без поля now): змінився — клієнтам шлемо сигнал."""
    import hashlib
    import json
    if _app_db is None:
        return ""
    p = public_payload(_app_db, 180, int(time.time()))
    p.pop("now", None)
    p.pop("updated_at", None)
    return hashlib.sha1(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest()[:12]


async def _api_stream(request: web.Request) -> web.StreamResponse:  # noqa: ANN001
    """SSE: події 'rev' при зміні даних + heartbeat. Дані НЕ передаються (клієнт бере /api/tracks, де діє публічна затримка)."""
    if _sse_clients[0] >= _SSE_MAX:
        return web.Response(status=503, text="busy")
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    _sse_clients[0] += 1
    last, idle = None, 0
    try:
        while True:
            if time.time() - _stream_rev["ts"] > 8:        # спільне обчислення для всіх клієнтів
                try:
                    _stream_rev["rev"] = await asyncio.get_running_loop().run_in_executor(None, _compute_rev)
                except Exception:  # noqa: BLE001
                    pass
                _stream_rev["ts"] = time.time()
            if _stream_rev["rev"] != last:
                last = _stream_rev["rev"]
                await resp.write(f"event: rev\ndata: {last}\n\n".encode()); idle = 0
            elif idle >= 25:
                await resp.write(b": hb\n\n"); idle = 0
            await asyncio.sleep(2); idle += 2
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _sse_clients[0] -= 1
    return resp


def _admin_guard(request: web.Request) -> web.Response | None:  # noqa: ANN001
    """None = доступ дозволено. Токен лише в заголовку (не в URL, щоб не потрапляв у логи)."""
    from airradar.monitoring import dashboard
    if not os.getenv("ADMIN_DASH_TOKEN"):
        return web.Response(status=404)
    if not dashboard.token_ok(request.headers.get("X-Admin-Token", "")):
        return web.Response(status=401, text="unauthorized")
    return None


async def _admin_health(request: web.Request) -> web.Response:  # noqa: ANN001
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    if _app_db is None:
        return web.json_response({"error": "db not ready"}, status=503)
    from airradar.monitoring import dashboard
    period = request.query.get("period", "24h")
    data = dashboard.collect(_app_db, period if period in dashboard.PERIODS else "24h")
    if request.query.get("format") == "html":
        return web.Response(text=dashboard.render_html(data), content_type="text/html")
    return web.json_response(data)


async def _admin_trace(request: web.Request) -> web.Response:  # noqa: ANN001
    denied = _admin_guard(request)
    if denied is not None:
        return denied
    if _app_db is None:
        return web.json_response({"error": "db not ready"}, status=503)
    from airradar.monitoring import incident_trace
    key = request.query.get("event", "")
    if key:
        return web.json_response({"timeline": incident_trace.timeline(_app_db, key),
                                  "replay": incident_trace.replay(_app_db, key) if request.query.get("replay") == "1" else None})
    return web.json_response({"events": incident_trace.search(_app_db, source=request.query.get("source", ""), text=request.query.get("q", ""))})


async def _api_risk(request: web.Request) -> web.Response:  # noqa: ANN001
    """Риск для точки: ?lat=&lon=&radius=10. Координаты не логируются и не сохраняются."""
    if _app_db is None:
        return web.json_response({"risks": [], "error": "db not ready"})
    try:
        lat, lon = float(request.query["lat"]), float(request.query["lon"])
        radius = max(2.0, min(50.0, float(request.query.get("radius", "10"))))
    except (KeyError, ValueError):
        return web.json_response({"risks": [], "error": "lat/lon required"}, status=400)
    public = public_mode() == "region"
    if public:
        # Публічний режим: обмеження частоти, фіксований радіус (інакше підбір радіуса дає відстань до цілі),
        # координата — на сітці ~10 км, дані із затримкою.
        if not _risk_allowed(request):
            return web.json_response({"risks": [], "error": "rate limit"}, status=429)
        lat, lon, radius = round(lat, 1), round(lon, 1), 10.0
    try:
        from track_engine import build_tracks, risk_for_point, risk_text
        now = int(time.time()) - (public_delay_s() if public else 0)
        tracks = build_tracks(_app_db.recent_observations(now - 3 * 3600), now, window_s=3 * 3600)
        risks = risk_for_point(tracks, (lat, lon), now, imminent_km=radius, watch_km=radius * 3)
        if public:
            return web.json_response({"now": _pv.round_ts(now), "risks": _pv.public_risk([{"level": r.level} for r in risks])})
        key = {t.id: t.key for t in tracks}
        return web.json_response({"now": now, "risks": [
            {"id": key.get(r.track_id, r.track_id), "level": r.level, "distance_km": r.distance_km,
             "bearing": r.bearing_from_point, "approaching": r.approaching, "eta_min": r.eta_min,
             "speed_kmh": r.speed_kmh, "text": risk_text(r)} for r in risks]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("API /api/risk error: %s", exc)
        return web.json_response({"risks": [], "error": "temporarily unavailable"}, status=503)


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
        resp.headers["Access-Control-Allow-Origin"] = "*" if not request.path.startswith("/admin/") else "null"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return resp

    app = web.Application(middlewares=[cors])
    app.router.add_get("/health", _health)
    app.router.add_get("/api/threats", _api_threats)
    app.router.add_get("/api/stats", _api_stats)
    app.router.add_get("/api/tracks", _api_tracks)
    app.router.add_get("/api/public/v1/regions", _api_public_regions)
    app.router.add_get("/admin/tracks", _admin_tracks)   # лише з X-Admin-Token, інакше 404/401
    app.router.add_get("/api/alerts-history", _api_alerts_history)
    app.router.add_get("/api/risk", _api_risk)
    app.router.add_get("/api/stream", _api_stream)
    app.router.add_get("/admin/health", _admin_health)   # лише з X-Admin-Token (ADMIN_DASH_TOKEN), інакше 404/401
    app.router.add_get("/admin/trace", _admin_trace)
    radar_path = Path(__file__).with_name("miniapp") / "radar.html"
    app.router.add_get("/radar", lambda request: web.FileResponse(radar_path))
    native_path = Path(__file__).with_name("miniapp") / "native.js"
    app.router.add_get("/native.js", lambda request: web.FileResponse(native_path))
    raions_path = Path(__file__).with_name("miniapp") / "raions.json"
    app.router.add_get("/raions.json", lambda request: web.FileResponse(raions_path))
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
