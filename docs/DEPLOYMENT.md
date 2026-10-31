# Розгортання та відкат

## Середовища
| | Тест/CI | Production |
|---|---|---|
| Де | GitHub Actions (`.github/workflows/ci.yml`), локально `.venv` | Proxmox CT 100, `/opt/airradar`, `airradar-bot.service` |
| Конфіг | `.env.example` (без секретів) | `.env` на хості (не в git, не в архіві деплою) |
| Секрети | відсутні, тестові значення в тестах | `.env`, `*.session`, `vapid_private.pem`, keystore — лише на хості/у власника |

## CI (`ci.yml`)
lint (синтаксис/невизначені імена; повний F для нових модулів) → pip-audit → перевірка гео-даних → secrets scan → pytest (unit+integration) → міграції на копії БД → smoke backup→restore → синтаксис JS карти → benchmark (інформаційно). Окремий job `android`: `npm ci && npm run sync`, `npm audit --audit-level=high`, `gradlew assembleDebug` (без keystore; підписаний release збирає власник локально, див. `app/README.md`). Pipeline не приховує падіння: кожен крок блокуючий, окрім benchmark.

## Передпольотний чекліст прод-деплою
1. Стан Git чистий / зміни закомічені (`git status`).
2. `systemctl start airradar-backup.service` у CT; `backup verify` останнього файлу.
3. `python -c "from airradar.ops import migrations; print(migrations.check_on_copy('/opt/airradar/airradar.db'))"` — на **копії** прод-БД: `ok: True`.
4. `.venv/bin/python -m pytest -q`.
5. `bash deploy_prod.sh` (PROXMOX_HOST=…; архів без секретів; міграції застосовуються при старті бота з попередньою копією `*.pre-migration.db`).
6. Після: `/health`, `/diag`, `/sources`, `/queue`, карта (`/api/tracks`), `journalctl -u airradar-bot -n 100`, тест-сповіщення **лише адміну** (`/selftest`).
Автоматичний прод-деплой з CI не налаштовано свідомо (ризик втрати даних/доступу).

## Відкат
- Код: `deploy_prod.sh` не зберігає попередню версію → перед деплоєм на хості: `tar czf /root/airradar-code-$(date +%F).tgz -C /opt/airradar --exclude=models --exclude=telegram_export --exclude='*.db*' .`; відкат: `tar xzf` + `systemctl restart airradar-bot`.
- БД: міграції лише додають таблиці/індекси, тож старий код працює на новій схемі. Повний відкат даних: `restore` (див. BACKUP_AND_RECOVERY.md) або `airradar.pre-migration.db`.
