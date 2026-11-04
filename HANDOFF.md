# AirRadar AI — контекст для нового чата

Вставь этот текст первым сообщением в новый чат (рабочая папка: /Users/admin/Downloads/AirRadar-AI-main).

---

Ты продолжаешь проект «AirRadar AI» (я говорю по-русски, бот и интерфейс — украинские). Всё ниже уже сделано и работает на проде; не переделывай, а развивай.

## Что это
Telegram-бот @AirRadar_AI_bot + канал @AirRadarAI: Telethon-аккаунт (@wzrdd2) читает 11 каналов о воздушных угрозах, парсит посты, строит треки целей, публикует в канал и рассылает подписчикам; карта-радар и Mini App на GitHub Pages; Android-приложение.

## Где что
- Локально: /Users/admin/Downloads/AirRadar-AI-main (не git). Тесты: `.venv/bin/python -m pytest -q -p no:cacheprovider` (≈409 проходят).
- Прод: Proxmox-хост 100.114.186.112 (нужен включённый Tailscale на Mac), контейнер CT 100, код в /opt/airradar, сервис `airradar-bot`; таймеры `airradar-radar-publish` (раз в 2 мин пушит tracks/stats/impacts.json в ветку radar-data), `airradar-history-sync-5min`, `airradar-backup` (ежедневная копия БД в /var/backups/airradar). Доступ: ssh root@хост + `pct exec 100 -- …`, `pct push`. Пароль root я дам в чате по запросу; в файлах его не храни, временные ssh-скрипты удаляй после деплоя.
- Деплой: tar изменённых файлов → scp на хост → `pct push 100` → `tar xzf` в /opt/airradar → `systemctl restart airradar-bot`. Миниапп/workflow коммитятся на сервере (`git add miniapp …; git push origin main`), Pages деплоит workflow `deploy-map.yml`.
- Pages: https://wizard732.github.io/AirRadar-AI/ (радар `/`, Mini App `/app.html`, APK `/AirRadar.apk`, данные tracks/stats/impacts.json).
- Android-проект: `app/` (Capacitor 8, своя фоновая служба `PushService` слушает ntfy-тему `ar-<код>`; SDK/JDK в ~/android-dev, подпись `app/android/airradar-release.jks` + `keystore.properties` — не терять!). Сборка: `cd app && npm run sync && cd android && ./gradlew assembleRelease` (JAVA_HOME=~/android-dev/jdk/Contents/Home, ANDROID_HOME=~/android-dev/sdk). APК копируется в `miniapp/downloads/AirRadar.apk`.

## Ключевые модули
`threat_parser.py` (разбор постов, газетир GeoNames `data/gazetteer_ua.tsv.gz`), `track_engine.py` (треки, курс, скорость, прогноз, `build_events`), `health_server.py` (`radar_payload`), `export_radar.py` + `radar_publish.sh`, `stats_engine.py`, `impact_log.py` (наслідки: жертви/место, пишутся из любых каналов, без задержки), `alarm_notices.py` (пост «ПОВІТРЯНА ТРИВОГА»/«ВІДБІЙ» по областям), `wave_digest.py` (одно живое сообщение о волне), `publish_dedup.py` (антиспам), `proximity.py` + `guard_places.py` (сторож, до 5 «місць», повтор попередження), `shelter_card.py`, `shelters.py` + `data/shelters_ua.tsv.gz` (6967 укриттів OSM), `family.py` (родина по приглашению, «Я в укритті»), `push_fcm.py` (пуши: ntfy по умолчанию, FCM спит без ключа), `menu_screens.py` + `bot_ui.py` + `guard_handlers.py` (всё меню на кнопках), `miniapp/radar.html`, `miniapp/app.html`, `miniapp/native.js`.

## Решения владельца (учитывать)
- Всё должно быть БЕСПЛАТНО: без Apple Developer, Firebase, платных туннелей. Входящих портов на сервере нет (всё через исходящие запросы и Pages).
- Правило «не публиковать точки прилётов» отменено: наслідки показываем сразу, место — город/область, без адресов объектов.
- Громкий звук из Telegram-бота невозможен — «пи-пи-пи»//sound убраны из бота. Громкий сигнал = Android-APK (канал «Тривога», звук alarm) и ntfy.
- iPhone: нативное приложение без платного аккаунта невозможно; есть ntfy из App Store + карта как PWA («На экран Додому»). iOS-проект в `app/ios` собирается без подписи; для установки на свой iPhone бесплатно — Xcode + Personal Team (7 дней, без пушей).
- Любые решения «публиковать/удалять/внешние сервисы» — сначала согласовывать, секреты не светить.

## Состояние и что осталось
- Проверено на эмуляторе Android 15: служба подключается к ntfy, тестовая тревога приходит в канал «Тривога» с моим звуком. НЕ проверено на реальном телефоне и при живой тревоге; `Не турбувати` без системного разрешения не пробивается.
- Неизвестно, доходят ли до меня живые посты о тревоге/влучанні в канал: следи за логами `journalctl -u airradar-bot` и качеством фильтров (alarm_notices, impact_log, publish_dedup).
- Идеи: подписать @wzrdd2 на каналы с подробными сводками (ОВА/ДСНС); токен alerts.in.ua (`ALERTS_IN_UA_TOKEN`) для надёжных сирен; Web Push для iPhone-PWA; точность прогнозов копится в `forecast_log`.
- Безопасность: пароль root светился в чате — поменять.
