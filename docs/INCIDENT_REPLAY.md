# Incident Replay

Код: `airradar/monitoring/incident_trace.py`, таблиця `event_trace` (мігр. №4).

## Що журналюється
Ключ події `"<джерело>:<id повідомлення>"`. Кроки: `received` (сирий текст ≤1000 симв.), `filter` (passed / no_keywords), `dedup`, далі за потреби `parse`, `geo`, `trust`, `safety`, `published|rejected`, `notify_*` — API `record()` готовий. Кожен рядок має час, джерело, ok/fail, дані, версії `parser_version` / `policy_version`.
**Інтегровано в конвеєр (`main._process_message`):** `received`, `filter`, `dedup`, `parse`, `geo`, `trust` (статус злиття/офіційність), `safety` (publishable + причина), `published` (шлях через агрегатор), `rejected` (з причиною: news/unknown_weapon/ignored_summary/not_publishable). **Не журналюється:** гілка «fallback без агрегатора» (лише тести) і кроки `notify_*` — доставка видна в `notification_queue` (`/queue`), але не пов'язана з ключем події в trace.

## Перегляд (лише адміни, дії пишуться в аудит)
- Бот: `/trace <ключ>` — часова лінія, `/replay <ключ>` — сухий прогін.
- HTTP: `GET /admin/trace?event=<ключ>[&replay=1]`, пошук `?q=Київ` або `?source=@канал`; заголовок `X-Admin-Token`.
## Безпечний replay
Береться збережений сирий текст → поточні `threat_parser.parse_post` і `safety_policy.check_publication` → порівняння з початковим результатом. Функція **не має доступу** до черги сповіщень і Telegram (`sent_notifications` завжди 0; тест `test_replay_is_dry_and_sends_nothing` перевіряє, що `notification_queue` порожня). Очищення: 30 днів (`retention`).
