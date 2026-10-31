# AirRadar — мобільний додаток (Android + iOS)

Додаток на Capacitor: карта радара, статистика, історія, наслідки (той самий код, що й сайт та Telegram Mini App) +
**нативні пуш-сповіщення з тривожним звуком** (канал Android `airradar_alarm`, звук `alarm.caf` на iOS) — без
налаштувань звуку в Telegram. Дані додаток бере з GitHub Pages (`tracks.json`, `stats.json`, `impacts.json`), вхідних
портів на сервері не потрібно.

## Як працюють пуші (без вхідного API)
1. Додаток генерує випадковий код і сам підписується на FCM-тему `u_<код>` (вкладка «Ще» → «Пуш-сповіщення»).
2. Користувач надсилає боту `/pair <код>` — бот зберігає тему (`push_fcm.py`) і видаляє повідомлення з кодом.
3. Сервер при тривозі/попередженні/небезпеці біля місця шле повідомлення в цю тему через FCM (iOS — через APNs, ключ у Firebase).

## Що потрібно від власника (без цього пуші не підуть)
| Що | Навіщо | Де взяти |
|---|---|---|
| Проєкт Firebase (безкоштовно) | FCM для Android та iOS | console.firebase.google.com |
| `google-services.json` | Android | Firebase → Project settings → Your apps → Android (`ua.airradar.app`) → кинути в `android/app/` |
| `GoogleService-Info.plist` | iOS | там само → iOS (`ua.airradar.app`) → перетягнути в Xcode (target App) |
| Ключ APNs `.p8` | пуші на iPhone | Apple Developer → Keys → APNs → завантажити у Firebase → Cloud Messaging |
| Apple Developer Program (99 $/рік) | підпис, TestFlight, App Store, APNs | developer.apple.com |
| Google Play Console (25 $ разово) | публікація Android | play.google.com/console |
| Service-account JSON | сервер надсилає пуші | Firebase → Project settings → Service accounts → Generate key |

Сервер: покласти service-account JSON, у `.env` додати `FCM_SERVICE_ACCOUNT=/opt/airradar/fcm-service-account.json`,
`pip install google-auth requests`, перезапустити `airradar-bot`.

## Збірка
```bash
cd app
npm install
npm run sync          # збирає www/ з ../miniapp і копіює в нативні проєкти
npm run ios           # відкриє Xcode (потрібен підпис: Signing & Capabilities → Team)
npm run android       # відкриє Android Studio
```
`AIRRADAR_DATA_BASE` (за замовчуванням `https://wizard732.github.io/AirRadar-AI/`) задає, звідки брати дані.

## Про звук
* **Android**: звук `res/raw/alarm.wav` прив'язаний до каналу «Тривога» (важливість MAX). Користувач може змінити його в
  налаштуваннях каналу; щоб пробивати «Не турбувати», потрібен дозвіл «Доступ до режиму Не турбувати» (системний).
* **iOS**: `alarm.caf` (до 30 с) грає, поки телефон не в беззвучному режимі; пуші мають `interruption-level: time-sensitive`.
  Повноцінні Critical Alerts (звук навіть у беззвучному режимі) потребують окремого дозволу Apple (entitlement) — подається
  у developer.apple.com, розглядається кілька тижнів.

## Стан перевірки
* iOS-проєкт компілюється для пристрою без підпису (`xcodebuild … CODE_SIGNING_ALLOWED=NO` — успішно).
* Android-проєкт згенеровано, але не збирався: на машині розробника немає JDK/Android SDK.
* Реальні пуші не перевірялись: потрібні Firebase-ключі.
