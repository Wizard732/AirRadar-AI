# Життєвий цикл

Два рівні, які не конфліктують:

1. **Стадії події** (`airradar/safety/event_lifecycle.py`, працює в продакшені): Created → Observed → Verified → Updated → Resolved → Archived. Стосується події/тривоги в цілому.
2. **Статус об'єкта** (`airradar/safety/object_lifecycle.py`, нове): окрема ціль — шахед, ракета — з 10 станами.

## Стани об'єкта
`detected`, `confirmed`, `tracking`, `stale`, `lost`, `intercepted`, `destroyed`, `fell`, `completed`, `expired`.

## Переходи (за `TRANSITIONS`)
- detected → confirmed, tracking, stale, lost, intercepted, destroyed, fell, completed
- confirmed → tracking, stale, lost, intercepted, destroyed, fell, completed
- tracking → stale, lost, intercepted, destroyed, fell, completed
- stale → tracking (дані відновились), lost, intercepted, destroyed, fell, completed
- lost → tracking, completed, expired
- intercepted, destroyed, fell, completed → expired
- expired — кінцевий

## Правила
- **Підтвердження.** `intercepted`, `destroyed`, `fell` вимагають джерела (`source`). Без нього — `TransitionError`. Джерело записується в `confirmed_by`.
- **Відсутність інформації ≠ знищення.** `evaluate_silence` переводить у `stale` (за замовчуванням 5 хв) і `lost` (25 хв), але ніколи в `destroyed`.
- **Анімація.** Тільки `tracking` і `confirmed`. `stale`, `lost` і закриті статуси — не анімуються.
- **Активний шар.** `stale` — видно з попередженням. `lost` — ще 10 хв, далі зникає. Закриті статуси з шару прибираються одразу.
- **Закриті не оживають.** Повторні згадки про `completed`/`destroyed` не змінюють запис.
- **Expired.** Закрита ціль старша 6 годин переходить у `expired`; історія лишається.
- **Довіра.** `low` (1 джерело), `medium` (2), `high` (3+). Рахується з незалежних джерел.
- **Історія.** Кожна зміна — `Change(ts, from, to, reason)`.

## Міграційний план (не виконано)
Поточний модуль — чистий, без БД. Щоб підключити до продакшену:
1. Нова таблиця `object_lifecycle(id PK, kind, status, detected_ts, last_seen_ts, closed_ts, sources_json, confirmed_by_json, history_json)`. Міграція — нова, без зміни `event_lifecycle`.
2. Пайплайн `threat_parser` → `observe` / `create`; `track_engine` → `evaluate_silence` на кожному тіку.
3. `radar_payload` віддає статус і `visible`; публічна відповідь застосовує затримку та агрегацію (район, не точка).
4. Підтвердження `destroyed` тільки з офіційного каналу або джерела з довірою `high`; поки такого правила немає в парсері — статуси з підтвердженням не виставляються автоматично.
