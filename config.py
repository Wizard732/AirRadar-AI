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

    # Telegram Bot API (публикация)
    bot_token: str
    target_channel: str

    # Локальный ИИ
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # Рантайм
    log_level: str = "INFO"
    dedup_ttl: int = 60
    http_timeout: int = 30
    healthcheck_interval: int = 300

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
        source_channels=source_channels,
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        dedup_ttl=int(os.getenv("DEDUP_TTL", "60")),
        http_timeout=int(os.getenv("HTTP_TIMEOUT", "30")),
        healthcheck_interval=int(os.getenv("HEALTHCHECK_INTERVAL", "300")),
    )
