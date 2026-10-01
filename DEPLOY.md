# Деплой AirRadar AI на VPS (бесплатно, 24/7)

Бот живёт на сервере и работает круглосуточно. Основной путь —
**Google Cloud e2-micro + Groq** (бесплатно навсегда, не засыпает, не грузит
сервер тяжёлой нейросетью).

---

## ИИ-бэкенд: почему Groq

Проект поддерживает два бэкенда сжатия текста (переключается `LLM_BACKEND` в
`.env`), но для VPS рекомендуется **Groq** — облачный API, который не требует
RAM под модель и отвечает за ~0.2 сек. Ollama (локальная) оставлена для запуска
на мощном компе/сервере, но на e2-micro (1 ГБ RAM) она не запустится.

| | Groq (облако) — рекомендуется | Ollama (локально) |
|---|---|---|
| `LLM_BACKEND` | `groq` | `ollama` |
| RAM на VPS | от 1 ГБ ✅ | 4–8 ГБ |
| Бесплатно | да (ключ console.groq.com) | да |

---

# Путь 1 (рекомендуемый): Google Cloud e2-micro + Groq

Бесплатный микро-инстанс навсегда. Не «засыпает». Хватит 1 ГБ RAM, потому что
ИИ работает в облаке Groq, а не на сервере.

## Шаг 1. Регистрация Google Cloud

1. Перейди на https://cloud.google.com/free → **Get started for free**.
2. Войди через Google-аккаунт.
3. Потребуется указать **банковскую карту** — для верификации.
   **Деньги НЕ спишут**: будет временный холд ~$1, который сразу вернётся.
   Free tier (e2-micro в регионе US) остаётся бесплатным навсегда.
4. Выбери страну, согласись с условиями. Аккаунт готов.

## Шаг 2. Создание сервера (VM Instance)

1. Открой консоль: https://console.cloud.google.com/
2. Слева в меню: **☰ → Compute Engine → VM Instances → Create VM instance**.
   (При первом заходе Compute Engine предложит включить — согласись, ~1 мин).
3. Заполни поля:
   - **Name:** `airradar`
   - **Region:** один из бесплатных: `us-west1`, `us-central1` или `us-east1`
     (зона — любая, например `us-central1-a`)
   - **Machine configuration:** General-purpose → Series **E2** →
     Machine type **e2-micro** (2 vCPU, 1 ГБ RAM, 30 ГБ диск — входит в free)
   - **Boot disk:** нажми Change → **Ubuntu 22.04 LTS** (или 24.04),
     тип Balanced, 30 ГБ
   - **Firewall:** поставь галочку **Allow HTTP/HTTPS traffic** (для надёжности)
4. В разделе **Identity and API access → Access scopes** оставь по умолчанию.
5. Раздел **SSH keys** (в самом низу, «Advanced» → «Security» → «SSH Keys»):
   - **ПРОЩЕ** не класть свой ключ, а после создания нажать **«Connect → Open
     in browser window»** (браузерный SSH работает сразу, без ключей).
6. Нажми **Create**. Через ~30 сек сервер готов. Скопируй его
   **External IP** (в колонке IP-адресов).

## Шаг 3. Подключение по SSH

**Вариант A (проще):** в консоли GCP нажми **Connect → Open in browser window**
на карточке инстанса. Откроется терминал прямо в браузере. Пользователь —
твой логин GCP (виден в строке приглашения, например `wizard_gmail_com`).

**Вариант B (через gcloud):** если установлен Google Cloud SDK:
```bash
gcloud compute ssh airradar --zone=us-central1-a
```

Дальше все команды выполняй **на сервере** в этом терминале.

## Шаг 4. Установка проекта (без Ollama)

```bash
# Системные пакеты
sudo apt update && sudo apt install -y python3 python3-venv git

# Клонировать репозиторий
git clone https://github.com/Wizard732/AirRadar-AI.git
cd AirRadar-AI

# Виртуальное окружение + зависимости (Telethon, aiohttp, dotenv)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Шаг 5. Получи бесплатный ключ Groq

1. Открой https://console.groq.com/keys (вход через Google/GitHub).
2. **Create API Key** → скопируй ключ (начинается с `gsk_...`).
3. Бесплатный лимит: ~14 400 запросов/день — с огромным запасом.

## Шаг 6. Создай `.env`

На сервере:
```bash
nano .env
```
Вставь (заполни все `replace_me` — значения индивидуальны, НИКОГДА не коммить реальные ключи):
```
TG_API_ID=replace_me_with_api_id
TG_API_HASH=replace_me_with_api_hash
SESSION_NAME=airradar
BOT_TOKEN=replace_me_with_bot_token
TARGET_CHANNEL=@AirRadarAI
SOURCE_CHANNELS=@rozvidkaneba,@KievskiyVanek,@poznyakyosokorkykharkivskiy,@truexakyiv,@raketa_trevoga,@kiev_levyy_bereg,-1003979438669
LLM_BACKEND=groq
GROQ_API_KEY=gsk_сюда_твой_ключ
GROQ_URL=https://api.groq.com/openai
GROQ_MODEL=llama-3.1-8b-instant
LOG_LEVEL=INFO
DEDUP_TTL=60
HTTP_TIMEOUT=30
HEALTHCHECK_INTERVAL=300
```
> 🔒 Реальные `TG_API_ID` / `TG_API_HASH` / `BOT_TOKEN` / номер телефона —
> только в `.env` на сервере (он в `.gitignore`). Если секрет попал в git —
> он считается скомпрометированным: отзови токен у @BotFather и выпусти новый,
> чистка файла из истории не помогает (история остаётся).
Сохранить: **Ctrl+O**, Enter, **Ctrl+X**.

## Шаг 7. Первый запуск — вход в Telegram

> ⚠️ **Перед этим ОСТАНОВИ бота на компе** (Ctrl+C в PowerShell). Две сессии
> одного аккаунта с разных IP = риск бана Telegram.

```bash
.venv/bin/python main.py
```
Telethon спросит:
- **Phone:** твой номер (он нигде не должен попадать в git)
- **Code:** код из SMS/TG
- **Password:** твой 2FA-пароль (если включён)

После строки `Подключено как @wzrdd2... Слушаю каналы…` → **Ctrl+C**.
Создастся файл `airradar.session`.

## Шаг 8. Автозапуск 24/7 (systemd)

```bash
WHOAMI=$(whoami)
WORKDIR=$(pwd)
sudo tee /etc/systemd/system/airradar.service > /dev/null <<EOF
[Unit]
Description=AirRadar AI — Telegram threat monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$WHOAMI
WorkingDirectory=$WORKDIR
ExecStart=$WORKDIR/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now airradar
```

Проверка:
```bash
sudo systemctl status airradar          # должно быть active (running)
sudo journalctl -u airradar -f          # логи в реальном времени (Ctrl+C — выйти)
```

🎉 **Готово.** Бот работает 24/7, перезапускается при сбое, стартует после
перезагрузки сервера. Комп можно выключать.

---

# Путь 2 (альтернатива): Oracle Cloud ARM + локальная Ollama

Если нужна полностью локальная обработка (без отправки текста в облако Groq) —
подойдёт Oracle Cloud ARM Ampere A1 (4 OCPU, 24 ГБ RAM бесплатно). Там
запускается локальная Ollama через `install.sh`. См. историю git этого файла
или используй `LLM_BACKEND=ollama`.

---

# ПРОД (актуально): Proxmox-хост + LXC контейнер 100 «AI-radar»

Бот живёт здесь. GCP-инструкция ниже — исторический путь установки.

## Топология

| Компонент | Значение |
|---|---|
| Хост | Proxmox VE 9.1.1, `ssh root@100.114.186.112` (пароль у владельца; UI `https://100.114.186.112:8006/#`) |
| Контейнер | **CT 100 «AI-radar»** (Ubuntu 24.04, 4 vCPU, 16 GB RAM, rootfs ~120G на `local-btrfs`) |
| Код в CT | `/opt/airradar` — git-репо, remote `git@github.com:Wizard732/AirRadar-AI.git` (SSH-ключ контейнера) |
| venv | `/opt/airradar/.venv` (Python 3.12) |
| Секреты | `/opt/airradar/.env` (в `.gitignore`, деплоем не трогается), сессия `airradar.session` |
| LLM | Ollama в CT100 (`LLM_BACKEND=ollama`, модель `airradar-qwen7b:q4`), Groq — резерв в `.env` |

## Сервисы systemd внутри CT100

| Юнит | Что делает |
|---|---|
| `airradar-bot.service` | основной бот: Telethon-слушатель + health_server (:8080, /map, /api/threats) |
| `airradar-map-sync.timer` | каждые 10 мин: `map_sync.sh` — пересборка `map_live.html` (generate_map.py) + commit + push в main (снапшот для GitHub Pages) |
| `airradar-history-sync-5min.timer` | каждые 5 мин: дозагрузка истории каналов в БД (sync_history.py) |
| `airradar-overnight-train.service` | ночное обучение (по расписанию) |

## Стандартный деплой (git push с локалки → CT100)

```bash
# 1. Локалка: коммит и пуш (main всегда должен быть впереди контейнера)
git push origin main

# 2. С хоста Proxmox — обновить код в контейнере и перезапустить бота:
#    (пароль root@100.114.186.112 — см. у владельца)
ssh root@100.114.186.112
pct exec 100 -- bash -c 'cd /opt/airradar && git fetch -q origin main && git reset -q --hard origin/main && git log --oneline -1'
pct exec 100 -- systemctl restart airradar-bot

# 3. Проверка:
pct exec 100 -- systemctl is-active airradar-bot          # active
pct exec 100 -- bash -c 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/threats?minutes=60'  # 200
pct exec 100 -- bash -c 'journalctl -u airradar-bot -n 20 --no-pager'
```

`git reset --hard` безопасен: секреты (`​.env`, `airradar.session`) в `.gitignore`
и физически не в репо. Незатреканный мусор в /opt/airradar reset не трогает.

**Важно:** `map_sync.sh` пушит снапшоты в main каждые 10 минут. Если пуш
с локалки отклонён (`fetch first`) — сначала `git pull --rebase origin main`,
разрешить конфликт в `EMBEDDED_THREATS` (брать серверную строку-снапшот),
затем пушить снова.

## Разовые операции (нестандартные)

```bash
# Исполнить команду внутри CT100 с хоста:
pct exec 100 -- <cmd>

# Копировать файл локалка → CT100 (через хост, sftp на хост + pct push):
#   sftp на 100.114.186.112 → /tmp/  →  pct push 100 /tmp/file /opt/airradar/path

# Интерактивная консоль в контейнер:
pct enter 100

# Расширение rootfs (теперь безопасно, ФС чистая):
pct resize 100 rootfs +10G
# Если онлайн-ресайз упал (флаг errors на ext4): stop → e2fsck -fy на
# loop-устройство образа с хоста → resize2fs → start (см. историю 2026-10-01).
```

## Старые аккуратно названные скрипты

- `deploy.sh` (в репо) — рассчитан на старый GCP-путь (`systemctl restart airradar`);
  в CT100 сервис называется `airradar-bot`, деплой по разделу выше.

---

# Шпаргалка команд (прод: Proxmox CT100, ssh root@100.114.186.112)

| Действие | Команда (на хосте Proxmox) |
|----------|---------|
| Статус бота | `pct exec 100 -- systemctl status airradar-bot` |
| Логи в реальном времени | `pct exec 100 -- journalctl -u airradar-bot -f` |
| Перезапустить бота | `pct exec 100 -- systemctl restart airradar-bot` |
| Остановить бота | `pct exec 100 -- systemctl stop airradar-bot` |
| Обновить код | `pct exec 100 -- bash -c 'cd /opt/airradar && git fetch -q origin main && git reset -q --hard origin/main'` + рестарт |
| Проверить API/карту | `pct exec 100 -- curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/map` |
| Снапшот карты сейчас | `pct exec 100 -- systemctl start airradar-map-sync.service` |
| Статус Ollama (CT100) | `pct exec 100 -- systemctl status ollama` |

---

# Troubleshooting

**Бот не публикует, в логе `CHAT_WRITE_FORBIDDEN`**
Бот @AirRadar_AI_bot не админ в целевом канале. Добавь его админом с правом
публикации постов в @AirRadarAI.

**Groq: `HTTP 401` / `invalid api key`**
Неверно `GROQ_API_KEY` в `.env`. Перепроверь ключ (начинается с `gsk_`).

**Groq: `HTTP 429` (rate limit)**
Превышен лимит запросов/токенов. Увеличь `DEDUP_TTL` в `.env`.

**Groq: `model_not_found`**
Устаревшее имя модели. Актуальный список: https://console.groq.com/docs/models.

**Бот не ловит сообщения из приватного канала**
В логе при старте должно быть `Доступно каналов: N/N` с этим каналом в списке.

**Telegram-сессия сломалась (частые разлогины)**
Сессия используется с двух IP сразу (комп + сервер). Останови бота на компе,
на сервере удали `airradar.session` и пройди вход заново (Шаг 7).

**GCP: превысил лимиты free tier (списали деньги)**
Проверь, что регион — `us-west1`/`us-central1`/`us-east1`, а тип машины —
именно `e2-micro` (не `e2-small`). Только `e2-micro` в этих регионах бесплатен.

---

# Безопасность на VPS

- Не клади `.env` и `airradar.session` в git (уже в `.gitignore`).
- Поставь firewall: `sudo apt install -y ufw && sudo ufw allow OpenSSH && sudo ufw enable`.
- Периодически: `sudo apt update && sudo apt upgrade -y`.
- Регулярно проверяй биллинг GCP: https://console.cloud.google.com/billing
  (должно быть $0 при соблюдении free tier).
