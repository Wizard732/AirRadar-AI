# Клиенты карты: что чем открывается

| Клиент | Файл | Как открывается |
|---|---|---|
| Радар (главная карта) | `miniapp/radar.html` | GitHub Pages `/` (`deploy-map.yml`: `site/index.html`); Mini App-вкладка «Карта» внутри `app.html` (iframe); APK (`app/www/radar.html`, собирается `npm run sync`); сервер бота `/radar` |
| Mini App-оболочка | `miniapp/app.html` | кнопка бота «📱 Mini App» → `<MAP_WEBAPP_URL>/app.html`; APK `index.html` |
| Старая карта бота | `miniapp/map.html` | сервер бота `/` и `/map` (`health_server`), снимок `map_live.html` → Pages `/snapshot.html` |

Кнопка бота «🗺 Карта» открывает `<MAP_WEBAPP_URL>/` (`menu_screens.site_base`). Если `MAP_WEBAPP_URL` указывает на GitHub Pages — это `radar.html`; если на туннель сервера — `map.html`. Значение `MAP_WEBAPP_URL` на проде нужно проверить (сервер был офлайн).

**Вывод:** основной клиент — `radar.html`. `map.html` — запасной; маршрутизацию не менял. Предложение: после проверки `MAP_WEBAPP_URL` отдавать на `/` и `/map` сервера `radar.html` (один клиент, один визуальный язык), а `map.html` оставить как `/legacy-map`.
