# Деплой AirRadar AI на VPS (бесплатно, 24/7)

Полный гайд: регистрация бесплатного VPS → установка → автозапуск.

## Выбор ИИ-бэкенда (важно — определяет требования к серверу)

Проект поддерживает **два бэкенда** сжатия текста, переключается одной строкой
в `.env` (`LLM_BACKEND=...`). Оба используют один и тот же системный промпт.

| | **Groq (облако)** — рекомендуется для VPS 24/7 | Ollama (локально) |
|---|---|---|
| `LLM_BACKEND` | `groq` | `ollama` |
| RAM на VPS | **от 1 ГБ** ✅ | 4–8 ГБ (только Oracle ARM free) |
| Скорость ответа | ~0.2 сек | 1–3 сек |
| Бесплатно | да, ключ на console.groq.com | да |
| Нагрузка на сервер | минимальная | высокая (модель в RAM) |
| Подходит любой VPS | **да** (GCP/AWS/Azure/Oracle x86) | нет (нужна ёмкость RAM) |

**Рекомендация:** для 24/7 на дешёвом/бесплатном VPS используй **Groq**.
Ollama оставь для запуска на своём компе или мощном сервере.

> 🔒 **Приватность (Groq):** текст постов уходит на серверы Groq. Это текст
> публичных каналов угроз (не личные секреты), что приемлемо. Если данные
> критичны — используй локальную Ollama.

---

## Быстрый путь: VPS + Groq (любой сервер от 1 ГБ RAM)

Подойдёт **любой** бесплатный/дешёвый VPS: Google Cloud e2-micro, AWS t2.micro,
Azure B1s, Oracle x86, или твой существующий сервер. Главное — Ubuntu/Debian
и 1 ГБ RAM.

### 1. Получи бесплатный Groq API ключ
1. Зайди на **https://console.groq.com/keys** (вход через Google/GitHub).
2. **Create API Key** → скопируй ключ (начинается с `gsk_...`).
3. Бесплатный лимит: ~14 400 запросов/день (с запасом для мониторинга каналов).

### 2. Создай VPS и подключись
- Запусти инстанс Ubuntu 22.04/24.04 (1 ГБ RAM достаточно).
- Скачай SSH-ключ, подключись: `ssh -i ключ ubuntu@<IP>`

### 3. Установи проект (без Ollama)
```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/Wizard732/AirRadar-AI.git
cd AirRadar-AI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
Обрати внимание: **Ollama не нужна**, модель не скачивается.

### 4. Настрой `.env`
```bash
nano .env
```
Вставь свои значения, **важные строки для Groq**:
```
LLM_BACKEND=groq
GROQ_API_KEY=gsk_твой_ключ_из_шага_1
GROQ_MODEL=llama-3.1-8b-instant
# остальные как на компе (TG_API_ID, BOT_TOKEN, SOURCE_CHANNELS, ...)
```

### 5. Первый запуск и вход в Telegram
```bash
.venv/bin/python main.py
```
Введи номер телефона + код → создастся `airradar.session` → **Ctrl+C**.

> ⚠️ **Перед деплоем на VPS останови бота на компе.** Две одновременные сессии
> одного аккаунта с разных IP — риск бана Telegram.

### 6. Автозапуск 24/7 через systemd
```bash
sudo cp deploy/airradar.service /etc/systemd/system/ 2>/dev/null || \
sudo tee /etc/systemd/system/airradar.service > /dev/null <<'UNIT'
[Unit]
Description=AirRadar AI — Telegram threat monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/AirRadar-AI
ExecStart=/home/ubuntu/AirRadar-AI/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now airradar
sudo journalctl -u airradar -f
```

---

## Альтернатива: VPS + локальная Ollama (нужен Oracle ARM, 24 ГБ RAM)

Если хочешь, чтобы ИИ работал **полностью локально** (без отправки текста в
облако) — нужен сервер с 4–8 ГБ RAM. Единственный бесплатный вариант —
**Oracle Cloud Free Tier (ARM Ampere A1)**: 4 ядра + 24 ГБ RAM навсегда.

### 1. Регистрация Oracle Cloud
1. https://www.oracle.com/cloud/free/ → **Start for free**.
2. Нужна карта для **верификации** (деньги **не списывают**).
3. Если регион пишет *«Out of capacity»* — смени регион (Frankfurt, Phoenix,
   Stockholm) или повтори через пару часов.

### 2. Создай инстанс
- **Shape:** Ampere `VM.Standard.A1.Flex` → 4 OCPU + 24 GB RAM.
- **Image:** Ubuntu 22.04/24.04.
- **SSH keys:** обязательно скачай private key (.key).
- **Networking:** галочка «Assign a public IPv4 address».

### 3. Установка одной командой
```bash
sudo apt update && sudo apt upgrade -y
git clone https://github.com/Wizard732/AirRadar-AI.git
cd AirRadar-AI
sudo bash install.sh   # ставит Python + Ollama + модель + systemd (~10 мин)
```

### 4. `.env`, вход в Telegram, автозапуск
```bash
nano .env               # LLM_BACKEND=ollama (уже по умолчанию)
.venv/bin/python main.py # телефон + код → Ctrl+C
sudo systemctl enable --now airradar
sudo journalctl -u airradar -f
```

---

## Обновление кода (для любого бэкенда)

После `git push` с компа — на сервере:
```bash
cd AirRadar-AI && sudo bash deploy.sh
```
`deploy.sh` сделает `git pull`, обновит зависимости (если надо) и перезапустит
сервис. `.env` и `airradar.session` не трогаются.

---

## Шпаргалка команд

| Действие | Команда |
|----------|---------|
| Статус бота | `sudo systemctl status airradar` |
| Логи в реальном времени | `sudo journalctl -u airradar -f` |
| Перезапустить | `sudo systemctl restart airradar` |
| Остановить | `sudo systemctl stop airradar` |
| Обновить код | `cd AirRadar-AI && sudo bash deploy.sh` |
| (только Ollama) Статус Ollama | `sudo systemctl status ollama` |

---

## Troubleshooting

**Бот не публикует, в логе `CHAT_WRITE_FORBIDDEN`**
Бот @AirRadar_AI_bot не админ в целевом канале. Добавь его админом с правом
публикации постов.

**Groq: `HTTP 401` / `invalid api key`**
Неверно `GROQ_API_KEY` в `.env`. Перепроверь ключ (начинается с `gsk_`).

**Groq: `HTTP 429` (rate limit)**
Превышен лимит запросов/токенов. Увеличь `DEDUP_TTL` в `.env` (чтобы повторы
не гоняли API), либо переключись на `LLM_BACKEND=ollama` временно.

**Groq: `model_not_found`**
Устаревшее имя модели в `GROQ_MODEL`. Актуальный список:
https://console.groq.com/docs/models — впиши существующее (например
`llama-3.1-8b-instant` или `qwen-2.5-...`).

**Бот не ловит сообщения из приватного канала**
В логе при старте должно быть `Доступно каналов: N/N` с этим каналом в списке.
Убедись, что аккаунт там подписчик.

**Telegram-сессия сломалась (частые разлогины)**
Сессия используется с двух IP сразу (комп + сервер). Останови бота на компе,
на сервере удали `airradar.session` и пройди вход заново.

---

## Безопасность на VPS

- Не клади `.env` и `airradar.session` в git (уже в `.gitignore`).
- Поставь firewall: `sudo ufw allow OpenSSH && sudo ufw enable`.
- (Только Ollama) Не открывай порт 11434 наружу — держи на `localhost`.
- Периодически: `sudo apt update && sudo apt upgrade -y`.
