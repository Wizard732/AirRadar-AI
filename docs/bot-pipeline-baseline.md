# Аудит конвейера AirRadar (read-only) — 2026-11-04

Код продукту не змінювався. Додано лише тести `test_pipeline_golden.py` (9 тестів: 8 проходять, 1 xfail фіксує підтверджений дефект). Підтверджене кодом/тестом відокремлено від ризиків.

## 1. Фактична схема
```
Telethon NewMessage (main.run → handler)
 → _process_message (main.py)
   0 OCR фото (Groq vision) → text; порожній текст → return
   1 clean_signature → matches_keywords (fast_filter)            [відкидає: «filtered»]
   2 DedupCache.is_duplicate (in-memory TTL 60 с + persistent 1 год) [відкидає: «duplicate»]
   3 is_channel_disabled(source,"military")                      [відкидає]
   4 parse_post (threat_parser) → kind news|foreign|stats|promo → відкидає (журнал inference_audit)
     impact_log (наслідки, лише офіційні) → publisher.send окремо
     alarm_notices.handle_post — офіційний старт/відбій по областях: окремі пости + _notify_subscribers + push
   5 classify_weapon; unknown → відкидає
   6 summarizer.summarize (LLM; при помилці повертає оригінал) — ДО публікації
   7 add_observations; extract_incident_fact; db.merge_incident_fact (fusion, підтвердження 2+ джерел / офіційне)
   8 publishable? (stage imminent, public_class; відбій завжди; unknown-stage лише при IN_FLIGHT_ONLY=0)
     CRITICAL (балістика, крилаті, КАБ…) від одного джерела — у БД/карту, але НЕ в канал (CRITICAL_NEEDS_CONFIRMATION)
   9 AlertAggregator.submit(ключ регіон+зброя, вікно 35 с, у пам'яті) → _publish_items
 → _publish_items: render_evidence_alert → _DEDUP.allow (publish_dedup) → Publisher.send/edit (Bot API → канал)
     → якщо published: _notify_subscribers → tg_delivery.deliver → notify_queue (SQLite, UNIQUE event_key+user+channel,
       backoff, FloodWait, TTL, відновлення після рестарту)
 → журнал: add_event, add_threat, save_entities, alert_end (відбій) / alert_start (лише corroborated|official + imminent)
Паралельні шляхи: sirens_poll (alerts.in.ua), sync_history кожні 5 хв (airradar-history-sync-5min.timer), radar-publish/map-sync timers.
```

## 2. Підтверджені дефекти (кодом/тестом)
> **Статус 2026-11-04: D1 і D2 ВИПРАВЛЕНО** (main.py `_publish_items` / `_process_message`, `PublishDedup.reserve/release`; тести `FixedDefects`, `test_publish_dedup_reserve_release`). D3 і R1 теж ВИПРАВЛЕНО (етап 16); R2, R3, R4 ВИПРАВЛЕНО (етап 17).

| # | Дефект | Доказ |
|---|---|---|
| D1 | **Розсилка в ЛС залежить від успіху публікації в канал.** `_publish_items` робить `return` при `published is None` до `_notify_subscribers`. Тимчасовий збій каналу → пост доїде з outbox, але підписники в ЛС НЕ отримають нічого (outbox шле лише в канал). Те саме у fallback-гілці `_process_message` (`if bot_client … and published`). | `test_pipeline_golden.py::KnownDefects` (xfail strict + фіксація поточної поведінки) |
| D2 | `_DEDUP.allow` має побічний ефект «запам'ятати як опубліковане» ДО фактичної відправки. Якщо `send` не вдалась (повернула None), повтор того ж повідомлення у вікні UAV_WINDOW_S блокується як «уже опубліковано». У парі з D1 — втрата для ЛС. | код `publish_dedup.allow` + `_publish_items` (ризик підтверджений читанням; окремого тесту немає) |
| D3 | `expire_stale_alerts()` викликається при формуванні екрана «🚨 Зараз» (`airradar/ui/views.py`): тривога без відбою мовчки закривається в БД через 6 год. Тобто «тиша» перетворюється на «закрито» як побічний ефект читання. | код views.py:543; `test_silence_does_not_close_alert` перевіряє лише <6 год |

## 3. Ризики (потребують тесту/рішення)
- R1 Агрегатор (вікно 35 с) тримає алерти лише в пам'яті: рестарт у вікні губить непублікований алерт. Повторного прогону немає (sync_history оновлює лише БД: observations/events/threats, **не публікує** ні в канал, ні в ЛС).
- R2 Усе тіло `_process_message` у широкому `try/except`: збій між fusion і aggregator.submit втрачає повідомлення без повтору (лише лог + `channel_error`).
- R3 `DedupCache.is_duplicate` фіксує відбиток ДО успішної обробки: помилка на наступних етапах → повтор того ж тексту в TTL відкинуто.
- R4 `_notify_subscribers` обходить підписників послідовно, `await deliver` на кожного (затримка пропорційна кількості підписників; при FloodWait — довга пауза для всіх).
- R5 Одиночна критична загроза (балістика/крилаті/КАБ) від одного джерела не публікується в канал — свідоме правило, але для користувача це «тиша»; видно лише на карті.
- R6 `_notify_regions` + `kyiv_zone` — розсилка за фактичною ціллю fusion; пост без розпізнаної цілі йде за всіма згаданими областями.
- R7 Неофіційні повідомлення НЕ підвищуються до «офіційно» (перевірено: `official` лише для source ∈ OFFICIAL_SOURCES; тест на ❔ у golden). Суперечливі повідомлення — окремого тесту немає.

## 4. Гарантії доставки (поточні)
- Канал: Publisher.send, тимчасовий збій → outbox (SQLite, TTL 10 хв, воркер 15 с). Постійна помилка → втрата з warning.
- ЛС: persistent notify_queue, at-least-once; блокований користувач — постійна помилка без ретраїв (виправлено на етапі 12).
- Після рестарту: `recover_after_restart` повертає «завислі» записи черги; агрегатор — ні (R1).

## 5. Покриття golden-тестами (зроблено)
Неофіційний одиночний пост → канал із ❔ і розсилка; дубль тексту з двох джерел → 1 пост; новина/не-загроза → нічого; вимкнене джерело → нічого; тиша не закриває тривогу (<6 год); офіційний старт+відбій → по 1 посту; відбій без відкритої тривоги → не публікується; D1 (xfail).
Ще потрібні: два незалежні неофіційні джерела (підвищення до corroborated), суперечливі повідомлення, неофіційний пост після відбою, збій одного підписника, 429/FloodWait наскрізно, рестарт під час вікна агрегатора, одночасна обробка дублів, пост без розпізнаного регіону.

## 6. Рекомендований порядок виправлень (ризик)
1. D1+D2: винести розсилку з-під умови `published`; `_DEDUP.allow` → «commit» лише після успішної відправки (низький ризик, є xfail-тест). 
2. D3: прибрати `expire_stale_alerts` з read-шляху; закривати у фоновому циклі з явною позначкою «закрито за таймаутом, не відбій» (середній).
3. R1: персистентний буфер агрегатора або публікація без очікування для критичних класів (середній/високий).
4. R3/R2: фіксувати dedup після успіху; повтор при збої між fusion і submit (середній).
5. R4: розсилка пачками з паралелізмом і лімітом (середній).
Файли для змін: main.py (`_process_message`, `_publish_items`), airradar/processing/publish_dedup.py, airradar/processing/dedup.py, aggregator.py, airradar/ui/views.py, airradar/delivery/tg_delivery.py.
