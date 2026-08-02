"""config.py — централизованная загрузка и валидация настроек приложения.

Все значения читаются из переменных окружения (для локального запуска — из файла
``.env`` рядом со скриптом через ``python-dotenv``). На старте конфиг
валидируется: при отсутствии обязательного поля поднимается понятная ошибка,
чтобы скрипт падал быстро и с подсказкой, а не посреди обработки сообщения.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    # python-dotenv опционален: в проде переменные могут задаваться через среду
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _require(name: str) -> str:
    """Вернуть обязательную переменную окружения или поднять ошибку с подсказкой."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения '{name}'. "
            f"Скопируй .env.example в .env и заполни значения."
        )
    return value


def _parse_channels(raw: str) -> list[str]:
    """Разобрать строку вида '@a, -100123, @b' в чистый список идентификаторов."""
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Иммутабельный контейнер всех настроек приложения."""

    # Telegram User API (чтение каналов)
    tg_api_id: int
    tg_api_hash: str
    session_name: str

    # Telegram Bot API (публикация + интерактивное меню)
    bot_token: str
    target_channel: str

    # Telegram user id администратора (кому доступно меню бота в личке).
    # 0 = меню отключено (бот только публикует).
    admin_id: int = 0

    # Путь к файлу SQLite-базы (журнал угроз/тревог для статистики и ETA)
    db_path: str = "airradar.db"

    # Локальный ИИ (бэкенд по умолчанию, тяжелый — нужна RAM под модель)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # Облачный ИИ Groq (лёгкий, для VPS 24/7 — не грузит сервер)
    groq_api_key: str = ""
    groq_url: str = "https://api.groq.com/openai"
    groq_model: str = "llama-3.1-8b-instant"

    # Какой бэкенд использовать: 'ollama' (по умолчанию) или 'groq'
    llm_backend: str = "ollama"

    # Рантайм
    log_level: str = "INFO"
    dedup_ttl: int = 60
    http_timeout: int = 30
    healthcheck_interval: int = 300

    # Включение Interests-модуля (новости по темам). По умолчанию включён.
    interests_enabled: bool = True

    # Дайджесты: часы (UTC) утра/вечера, когда собирать и слать сводку.
    # Пусто = дайджесты отключены (только мгновенная выдача).
    digest_morning_hour: int = 8
    digest_evening_hour: int = 20

    # Исходные каналы мониторинга (по умолчанию пусто — проверяется в load_settings)
    source_channels: list[str] = field(default_factory=list)


def load_settings() -> Settings:
    """Прочитать и провалидировать настройки окружения.

    Поднимает ``RuntimeError`` с понятным сообщением, если чего-то не хватает
    или значение некорректно (например, нечисловой ``TG_API_ID``).
    """
    api_id_raw = _require("TG_API_ID")
    try:
        tg_api_id = int(api_id_raw)
    except ValueError as exc:  # некорректный api_id
        raise RuntimeError(
            f"TG_API_ID должен быть числом, получено: {api_id_raw!r}"
        ) from exc

    source_channels = _parse_channels(os.getenv("SOURCE_CHANNELS", ""))
    if not source_channels:
        raise RuntimeError(
            "SOURCE_CHANNELS пуст — укажи хотя бы один исходный канал в .env"
        )

    return Settings(
        tg_api_id=tg_api_id,
        tg_api_hash=_require("TG_API_HASH"),
        session_name=os.getenv("SESSION_NAME", "airradar"),
        bot_token=_require("BOT_TOKEN"),
        target_channel=_require("TARGET_CHANNEL"),
        admin_id=int(os.getenv("ADMIN_ID", "0")),
        db_path=os.getenv("DB_PATH", "airradar.db"),
        source_channels=source_channels,
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        groq_url=os.getenv("GROQ_URL", "https://api.groq.com/openai"),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        llm_backend=os.getenv("LLM_BACKEND", "ollama").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        dedup_ttl=int(os.getenv("DEDUP_TTL", "60")),
        http_timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
        healthcheck_interval=int(os.getenv("HEALTHCHECK_INTERVAL", "300")),
        interests_enabled=os.getenv("INTERESTS_ENABLED", "1").strip() not in ("0", "false", "no"),
        digest_morning_hour=int(os.getenv("DIGEST_MORNING_HOUR", "8")),
        digest_evening_hour=int(os.getenv("DIGEST_EVENING_HOUR", "20")),
    )
