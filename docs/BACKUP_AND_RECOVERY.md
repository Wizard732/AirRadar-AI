# Резервне копіювання та відновлення

Код: `airradar/ops/backup.py` (CLI: `python -m airradar.ops.backup …`), точка входу таймера — `backup_db.py`.

## 1. Що резервується
| Об'єкт | Як | Примітки |
|---|---|---|
| SQLite (`DB_PATH`) | онлайн-копія через `sqlite3.backup` (бот не зупиняється, WAL-безпечно) | `PRAGMA integrity_check` копії обов'язковий — інакше backup падає |
| Користувачі, підписки, фільтри, геозони, адміни, аудит | усередині БД | |
| Конфіг | `env.redacted` — `.env` зі значеннями секретів `<REDACTED>` | ключі лишаються (структура), значення — у менеджері паролів |
| Локальні дані | `data/` (укриття, gazetteer), `miniapp/vapid.json` (публічний ключ) | |
| Критичні файли | `requirements.txt`, `deploy_prod.sh`, `.env.example`, systemd unit/timer бота й backup | код — у git |
| Telegram-сесія `*.session` | **лише якщо задано `BACKUP_PASSPHRASE`** | архів тоді шифрується AES-256 (openssl, PBKDF2) |
| `vapid_private.pem` (Web Push) | так само — лише в зашифрованому архіві | |
| **НЕ входить** | Android keystore (`*.jks`, `keystore.properties`), `telegram_export`, `models/` | keystore зберігати окремо; втрата = неможливість оновити APK |

## 2. Де зберігається
`BACKUP_DIR` (за замовчуванням `/var/backups/airradar`, права `0700`, файли `0600`), імена `airradar-backup-YYYYmmdd-HHMMSS.tar.gz[.enc]`.
**Рекомендація:** додатково копіювати каталог поза хост (інший диск/машина) — копія на тому ж диску не захищає від його втрати (дію виконує власник).

## 3. Як часто
`airradar-backup.timer` — щодня о 03:40 (`OnCalendar`). Змінити розклад: `systemctl edit airradar-backup.timer` →
```
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:40:00,15:40:00
```
Ротація: `BACKUP_KEEP` (7) останніх і ліміт сумарного розміру `BACKUP_MAX_TOTAL_MB`. Попередження, якщо одна копія > `BACKUP_MAX_SINGLE_MB`.

## 4. Сповіщення
Успіх і помилка надсилаються адміну через Bot API (`BACKUP_NOTIFY_SUCCESS=0` вимикає успішні). Додатково алерт `backup_stale`, якщо копії немає довше `ALERT_BACKUP_MAX_AGE_S` (36 год).

## 5. Перевірка цілісності
```bash
python -m airradar.ops.backup verify /var/backups/airradar/airradar-backup-….tar.gz     # sha256 усіх файлів + integrity_check БД
python -m airradar.ops.backup list
```
Автоматично виконується після кожного створення.

## 6. Відновлення системи (повний сценарій)
1. **Не перезаписуйте живу систему.** Спершу перевірте копію на окремій машині/каталозі: `python -m airradar.ops.backup restore-test <файл>` (розпаковує у тимчасову теку, відкриває БД штатним `Database`, застосовує міграції, робить `integrity_check`).
2. Зупиніть бота: `systemctl stop airradar-bot`.
3. Збережіть поточний стан: `cp /opt/airradar/airradar.db /opt/airradar/airradar.db.before-restore`.
4. Відновіть: `BACKUP_PASSPHRASE=… python -m airradar.ops.backup restore <файл> /opt/airradar/restore-tmp [--with-session]`. Існуючі файли **не перезаписуються** (з'являється суфікс `.restored`).
5. Перегляньте результат і вручну замініть файли (`mv restore-tmp/airradar.db.restored …` або, якщо файлу не було, він уже на місці).
6. Секрети: `env.redacted` НЕ відновлює значення — заповніть `.env` з менеджера паролів.

### Відновити тільки базу
```bash
systemctl stop airradar-bot
python -m airradar.ops.backup restore <файл> /tmp/ar-restore      # без --with-session
sqlite3 /tmp/ar-restore/airradar.db "PRAGMA integrity_check;"      # має бути ok
cp /tmp/ar-restore/airradar.db /opt/airradar/airradar.db && rm -f /opt/airradar/airradar.db-wal /opt/airradar/airradar.db-shm
systemctl start airradar-bot
```
### Відновити Telegram-сесію
Потрібен зашифрований архів і `BACKUP_PASSPHRASE`: `… restore <файл> <каталог> --with-session` → скопіювати `airradar.session` у робочий каталог (права `0600`). Якщо Telegram анулював сесію (вхід з іншого місця) — виконайте новий вхід `python tg_login.py`; копія сесії допомагає лише поки вона дійсна.

## 7. Перевірка працездатності після відновлення
- `systemctl is-active airradar-bot`, `curl -s localhost:8080/health` → `{"status":"ok"}`;
- у боті `/diag` (БД ok, схема остання, backup < 1 доби), `/sources` (джерела оновлюються), `/selftest` (тест лише адміну);
- карта `/api/tracks` відповідає; `journalctl -u airradar-bot -n 50` без помилок.

## 8. Статус перевірки відновлення
- ✅ Автотести (`test_ops_reliability.py`): створення, шифрування, redaction секретів, відмова при неправильному пароль/пошкодженні/path traversal, ротація, restore без перезапису, `restore_test` на синтетичній БД; CI виконує smoke backup→restore.
- ⏳ **Не виконано:** відновлення справжньої прод-копії (~700 МБ) на окремому стенді. Backup **не вважати повноцінним**, доки власник не виконає п. 6 (`restore-test`) на тестовій машині з реальним архівом і не підтвердить.
