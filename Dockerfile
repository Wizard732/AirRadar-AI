# Dockerfile для AirRadar AI (Koyeb / любой контейнерный деплой)
# Лёгкий образ на базе slim Python. Telethon работает без системных зависимостей,
# поэтому достаточно установить зависимости из requirements.txt и запустить main.py.

FROM python:3.12-slim

# Рабочая директория
WORKDIR /app

# Сначала копируем только зависимости — этот слой кешируется и не пересобирается
# при изменении кода, ускоряя деплои.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код проекта
COPY . .

# PaaS-платформы (Koyeb) передают секреты через переменные окружения.
# .env в образ не кладём — он пробрасывается через env vars сервиса.
# airradar.session монтируется как ephemeral volume или пересоздаётся.

# Порт для health-сервера (см. health_server.py). Koyeb ждёт, что приложение
# слушает порт из переменной PORT. ExpoSE только как документация.
EXPOSE 8080

# Запуск бота. Health-сервер стартует внутри main.py на $PORT.
CMD ["python", "main.py"]
