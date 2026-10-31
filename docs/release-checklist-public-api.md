# Релиз публичного контракта карты — чек-лист

Статус: **не выкатывалось.** Релиз только после подтверждения архитектора (ChatGPT) и владельца.

## 1. До релиза (локально)
- [x] Полный набор тестов: `.venv/bin/python -m pytest -q -p no:cacheprovider` → 613 passed.
- [x] `node --check` встроенных скриптов `radar.html`, `map.html`, `app.html`.
- [x] Примеры JSON «было/стало»: `docs/public-json-examples.md`.
- [ ] Ревью diff архитектором.

## 2. Сервер (Proxmox CT, `airradar-bot`)
1. Бэкап БД: `pct exec 100 -- systemctl start airradar-backup.service`, проверить `Result=success`.
2. `PROXMOX_HOST=… bash deploy_prod.sh` (тесты → архив → распаковка → рестарт).
3. На проде проверить (`curl` с самого CT, токен не нужен):
   - `/api/tracks?minutes=180`: `public=true`; нет `speed_kmh`≠null, `heading`≠null, `dest`≠null, `forecast`≠[]; у `points` ровно 1 точка; нет чисел с 3+ знаками после запятой.
   - `/api/public/v1/regions`: нет ключей `lat, lon, points, now, id, speed_kmh, heading, forecast, dest`.
   - `/api/threats?minutes=120`: нет `track, origin, destination`; `text` вида «Тип — область»; `reports[].text` = «Повідомлення користувача».
   - `/api/risk?lat=50.45&lon=30.52`: в элементах только `level`, `text`; 21-й запрос за минуту → 429.
   - `/admin/tracks` без заголовка → 401 (или 404, если `ADMIN_DASH_TOKEN` не задан).
   - `/api/stream`: приходит `event: rev`.
4. Убедиться, что `TRACKS_PUBLIC_DELAY_MIN` в `.env` прода либо не задан (→ 10 мин), либо задан осознанно.

## 3. GitHub Pages
1. Коммит в `main` (код + docs + тесты), push → workflow `Deploy radar and map`.
2. Дождаться следующего прогона `airradar-radar-publish.timer` (≈2 мин): `radar-data` перезаписывается одним коммитом, появляется `public-regions.json`.
3. Проверить `https://wizard732.github.io/AirRadar-AI/tracks.json`, `/public-regions.json`, `/impacts.json` теми же проверками, что в п. 2.3.
4. CDN Pages держит кэш ≈10 мин — повторная проверка через 15 мин.

## 4. APK — что пересобрать
- `app/www/radar.html` сейчас старый (отличается от `miniapp/radar.html`); `app/www/native.js`, `raions.json` совпадают.
- Шаги: `cd app && npm run sync` (копирует `miniapp/*` в `www` через `tools/build-web.mjs` и делает `cap sync`), поднять `versionCode` (12 → 13) и `versionName` в `app/android/app/build.gradle`, `./gradlew assembleRelease`, подписать тем же keystore, положить `miniapp/downloads/AirRadar.apk`.
- Старые APK продолжают работать на совместимом формате `/api/tracks` (проверено тестом на коде `app/www/radar.html`); точных данных они больше не получают, потому что фильтрация на сервере.
- Старые установки хранят старый `localStorage['ar_last_tracks']` до обновления; новая версия его удаляет.

## 5. Откат
- Сервер: `git revert <коммит>` в рабочей копии → `deploy_prod.sh`. Включить старый формат переменной окружения **нельзя** (переключателя нет).
- Pages: revert-коммит в `main` пересобирает сайт; данные `radar-data` пересоздадутся таймером.

## 6. Что гарантированно не отзывается
Ранее скачанные копии публичных файлов, архивы третьих лиц (web.archive и т.п.), старые установки APK с их локальным кэшем до обновления.
