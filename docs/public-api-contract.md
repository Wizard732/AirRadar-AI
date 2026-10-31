# Этап 0 — публичные данные карты: аудит и предлагаемый контракт

Статус: **предложение, production API не менялся.** Нужно решение владельца и согласование с архитектором (ChatGPT).

## 1. Инвентаризация публичных каналов
| Канал | Файл | Что отдаёт | Кто читает |
|---|---|---|---|
| `GET /api/tracks` | `health_server._api_tracks`, `radar_payload` | треки, события, сирены, наследия, лента | `radar.html`, APK |
| `tracks.json` (Pages) | `export_radar.py` → ветка `radar-data` (force-push одним коммитом) → `deploy-map.yml` | тот же `radar_payload` | `radar.html` на github.io |
| `GET /api/stream` (SSE) | `_api_stream` | только хэш `rev` + heartbeat, данных нет | `radar.html` |
| `GET /api/threats` | `_api_threats` | события с `track` (курс, скорость, from/to), `origin`, `destination` | `map.html` |
| `GET /api/risk`, `/api/stats`, `/api/alerts-history` | `health_server` | риск по точке, статистика, журнал тревог | `radar.html`, `map.html` |
| `impacts.json`, `stats.json`, `alerts_hist.json` | `export_radar.py` | наследия (с задержкой `IMPACT_DELAY_MIN`), статистика | `radar.html` |
| `/raions.json`, `/radar`, `/`, `/map` | статические файлы | контуры районов, страницы | все |

SSE безопасен по содержимому (только `rev`), но сигнал «данные изменились» без задержки сам по себе раскрывает момент появления события.

## 2. Поля `/api/tracks` → `tracks[]` (синтетический пример текущего ответа)
```json
{
  "now": 1700000000, "window_min": 180,
  "tracks": [{
    "id": "t-0001", "weapon": "shahed", "profile": "", "status": "active", "count": 3,
    "sources": 2, "official": null,
    "speed_kmh": 180.0, "speed_source": "typical",
    "heading": 45, "heading_text": "північний схід",
    "now": {"lat": 50.0000, "lon": 30.0000, "estimated": true},
    "points": [{"ts": 1699999400, "lat": 49.9000, "lon": 29.9000, "count": 3, "place": "Місто-А", "precision": "approx"}],
    "forecast": [{"name": "Місто-Б", "lat": 50.1000, "lon": 30.1000, "eta_min": 20, "km": 25.0}],
    "dest": {"lat": 50.4500, "lon": 30.5200, "name": "Місто-Б"}
  }],
  "events": [], "impacts": [], "sirens": null, "alerts": [], "feed": [], "accuracy": null
}
```
Значения синтетические.

## 3. Оценка чувствительности полей
| Поле | Источник | Где используется | Чувствительно | Публично |
|---|---|---|---|---|
| `points[].lat/lon/ts/place` | `track_engine` | след на карте, воспроизведение | **да** (история траектории) | убрать |
| `now.lat/lon` | `position_at` | положение маркера | **да** (live-позиция) | заменить центроидом района |
| `speed_kmh`, `speed_source` | `track_engine` | карточка | **да** | убрать |
| `heading`, `heading_text` | `track_engine` | поворот модели, карточка | **да** | убрать |
| `forecast[]` | `forecast_places` | прогноз пути | **да** (маршрут) | убрать |
| `dest` | `track_engine` | подсветка цели | **да** (направление) | заменить признаком «район в зоне угрозы» без траектории |
| `weapon`, `count` | парсер | категория, число | категория — нет; число — грубо | категория + диапазон (1, 2–5, 6+) |
| `status` | `track_engine` | статус | нет | статус lifecycle |
| `sources` | трек | доверие | нет | диапазон (1, 2, 3+) |
| `official` | сирены | статус | нет | оставить |
| `feed[].text` | `public_text` | лента | нет (правила №6) | оставить |
| `impacts[]` | `impact_log` | прилёты | решение владельца (задержка `IMPACT_DELAY_MIN`) | без изменений до решения |
| `/api/threats`: `track`, `origin`, `destination` | `track_speed` | карта 1 | **да** | убрать для публичного режима |

## 4. Статусы: `track_engine` ↔ `object_lifecycle`
| `track_engine.Track.status` | Предлагаемое состояние lifecycle |
|---|---|
| `active` (свежие точки) | `tracking` (при 2+ источниках — `confirmed`) |
| `stale` (>25 мин без точек) | `stale`, далее `lost` |
| `loitering` | `tracking` с признаком «кружит» (отдельное поле, не статус) |
| (нет в `track_engine`) | `detected`, `intercepted`, `destroyed`, `fell`, `completed`, `expired` — только через `object_lifecycle.close/evaluate_silence` с подтверждением |

## 5. Предлагаемый публичный контракт (`/api/public/v1/regions`, `public-regions.json`)
```json
{
  "updated_at": 1700000000,
  "delay_min": 10,
  "regions": [{
    "region": "dnipropetrovska", "raion": "Дніпровський",
    "category": "drone",
    "status": "tracking",
    "count_bucket": "2-5",
    "sources_bucket": "2",
    "first_seen": 1699999200, "last_update": 1699999800,
    "stale": false
  }]
}
```
Правила:
- нет `lat/lon`, `points`, `speed`, `heading`, `forecast`, `dest`;
- время округляется до 5 минут и сдвигается на `delay_min`;
- позиция модели на карте = центроид района/области (фиксирован, не «живая точка»);
- категория: `drone`, `missile`, `other`; статус — из lifecycle;
- приоритет цвета: ракета > дрон > прочее.

Закрытый режим (`/admin/tracks`, заголовок `X-Admin-Token`, уже используется для `/admin/*`): текущий полный `radar_payload`.

## 6. Задержка и архив
- Рекомендуется `TRACKS_PUBLIC_DELAY_MIN ≥ 10` по умолчанию (решение владельца; сейчас `0`).
- Задержка не заменяет урезание полей: публичный контракт не должен содержать координатной истории вообще.
- `radar-data` force-push одним коммитом: старые версии недоступны по ветке, но кэш CDN и клиенты могут хранить копии; хранение в `sw.js` и `localStorage` проверить на предмет координатных данных.

## 7. Тесты, подтверждающие безопасность
1. Рекурсивный обход публичного JSON: запрещённые ключи `points, now, speed_kmh, heading, heading_text, forecast, dest, track, origin, destination, lat, lon`.
2. Регулярка по значениям: нет чисел с 3+ знаками после запятой в диапазонах координат Украины.
3. Те же проверки для `/api/threats`, `tracks.json`, `public-regions.json`.
4. SSE: событие содержит только `rev`.
5. Без токена `/admin/tracks` → 401/404; с токеном — полный payload.
6. Сканирование `radar.html`, `map.html`, `sw.js` на ссылки к закрытым endpoint.

## 8. Затрагиваемые компоненты
`health_server.py`, `export_radar.py`, `radar_publish.sh`, `track_engine.py`, `track_speed.py`, `.github/workflows/deploy-map.yml`, `miniapp/radar.html`, `miniapp/map.html`, `miniapp/sw.js`, `app/www/*` (копия для APK), тесты `test_live_map.py` и новые.

## 9. Схема после рефакторинга
```
Парсер / геокодер / сирены ──► БД ──► track_engine ──► object_lifecycle (статусы, история)
                                                  │
                         ┌────────────────────────┴───────────────────────┐
                         ▼                                                ▼
              private_payload (полный)                         public_payload (агрегат по районам,
              /admin/tracks (токен)                            задержка, без координат)
                                                                      │
                                              ┌───────────────────────┼─────────────────────┐
                                              ▼                       ▼                     ▼
                                       /api/public/v1/regions   public-regions.json       SSE rev
                                              │                       │ (Pages)
                                              └──────────┬────────────┘
                                                         ▼
                                          общий модуль map-core.js (категории, цвета, статусы)
                                                  │                   │
                                              map.html           radar.html (+APK)
```
