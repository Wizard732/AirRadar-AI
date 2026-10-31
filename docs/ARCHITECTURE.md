# Архітектура AirRadar AI

```
Telegram-джерела (Telethon) → main._process_message
   → clean_signature → fast_filter → dedup → classify/stage → ai_summarizer (Ollama/Groq, ai_guard)
   → threat_parser (+gazetteer/geo) → incident_fusion / trust → safety_policy → publisher (канал)
   → підписники (bot_ui) / push (ntfy, FCM, Web Push) → SQLite (database.py)
   → health_server: /api/tracks, /api/threats, /health, /admin/* → карта (miniapp/radar.html, GitHub Pages)
```
Паралельно: `incident_trace` пише кроки конвеєра, `metrics` — тривалості, `alert_loop` — алерти адмінам.

## Пакет `airradar/` (нова структура; у корені лишаються шими для зворотної сумісності)
| Домен | Модулі |
|---|---|
| `safety` | safety_policy, trust, notification_rules, event_lifecycle, personal_status |
| `delivery` | admin_notify, webpush_push, **notify_queue** (черга, ретраї, backoff) |
| `monitoring` | health_checks, ai_analyzer, moderation, benchmark, **metrics, alerts, alert_loop, dashboard, incident_trace, diag_commands** |
| `security` | **admin_access** (аудит, токени), **ai_guard** |
| `ops` | **backup, migrations, retention** |
| `product`, `database` | як було |
Принцип міграції коду: нові модулі — у `airradar/<домен>/`; старий модуль замінюється шимом (`sys.modules[__name__] = _m`). Великі файли (`main.py` 84 КБ, `database.py` 139 КБ) свідомо не розбивались одним кроком; план — виносити по домену з тестами між кроками.

## База даних
SQLite, WAL, `busy_timeout=30000`, один `threading.Lock` на з'єднання (`check_same_thread=False`). Міграції: `schema_migrations` + `airradar/ops/migrations.py` (лише додаткові, backup перед застосуванням, `check_on_copy` у CI/деплої). Нові таблиці: `admin_audit`, `admin_tokens`, `notification_queue`, `event_trace`, `stage_metrics`, `alert_state`. Очищення: `retention.run` (trace 30 дн., метрики 30 дн., завершена черга 14 дн.; дані користувачів і журнал угроз не чіпаються).
Обмеження: немає FK у історичній схемі; один writer-lock (виміряно: 8 потоків × 200 операцій без помилок, див. `scripts/loadtest.py`). Redis/PostgreSQL не потрібні за поточних вимірів.

## Конвеєр сповіщень
`notify_queue`: `pending → sending → sent | retrying → failed | cancelled | expired`; унікальність `(event_key, user_id, channel)`; backoff 5·2ⁿ с (≤10 хв), `Retry-After` від rate-limit поважається; `recover_after_restart` повертає `sending` у `retrying`; `cancel_for_event` при завершенні події.
**Статус інтеграції:** розсилка підписникам у Telegram (`main._notify_subscribers`) іде через `airradar/delivery/tg_delivery.py`: запис у чергу (ключ = пост+текст+10-хв вікно) → негайна спроба → при збої `retrying` (backoff; FloodWait/`Retry-After` поважається) → фоновий `retry_worker` (задача `notify-retry`). Після перезапуску `recover_after_restart` повертає застряглі `sending`. Старий журнал `notification_delivery` ведеться як і раніше. **Ще не на черзі:** FCM/ntfy/Web Push (`push_fcm`, `webpush_push`) і пости в канал — вони йдуть старим шляхом.

## AI-провайдери
`airradar/intelligence/providers.py` — `ResilientSummarizer`: таймаут, retry з backoff, circuit breaker, денний ліміт викликів (контроль вартості), fallback-провайдер, валідація виходу (`ai_guard`), статистика. **Вимкнено за замовчуванням** (щоб не змінювати поведінку); вмикається `AI_RESILIENT=1`, `AI_FALLBACK_BACKEND=…` або `AI_DAILY_LIMIT_PRIMARY>0` (див. `.env.example`). Перенос парсингу з Ollama на API **не виконувався**; порівняння — `scripts/compare_providers.py` (потрібні живі Ollama/Groq; на цій машині Ollama недоступна, тож вимірів немає).
