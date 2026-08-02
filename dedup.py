"""dedup.py — TTL-кеш дедупликации повторных сообщений.

Несколько мониторинговых каналов часто репостят одно и то же сообщение с
разницей в секунды. Чтобы не публиковать дубли, ведём in-memory кеш хешей
текста с TTL (~60с). Хеш берётся от нормализованного текста (lower + свернённые
пробелы), чтобы совпадали сообщения, отличающиеся только регистром/лишними
пробелами. Блокировка не нужна: всё работает в одном event loop.
"""

from __future__ import annotations

import hashlib
import re
import time

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Привести текст к каноничному виду: нижний регистр + одиночные пробелы."""
    return _WHITESPACE.sub(" ", text.strip().lower())


def _fingerprint(text: str) -> str:
    """Стабильный отпечаток нормализованного текста (sha256)."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


class DedupCache:
    """Гибридный кеш дедупликации: in-memory + опционально SQLite.

    In-memory даёт мгновенную проверку (без I/O). SQLite добавляет
    персистентность — повтор, репостнутый в другом канале через 40 минут
    или после рестарта бота, тоже будет отсечён.

    Не thread-safe, но безопасен в рамках одного asyncio event loop,
    чего достаточно для данного приложения.
    """

    def __init__(self, ttl: int, db=None) -> None:
        # ttl в секундах для in-memory слоя (короткое окно, быстрый ответ).
        self._ttl = ttl
        self._seen: dict[str, float] = {}
        # Опциональная ссылка на Database для персистентной проверки.
        self._db = db
        if db is not None:
            # SQLite-окно делаем больше in-memory — чтобы ловить поздние репосты.
            db.set_dedup_window(max(ttl, 3600))

    def is_duplicate(self, text: str) -> bool:
        """Вернуть True, если такое сообщение уже приходило.

        Проверка двухуровневая:
          1) быстрый in-memory кеш (мгновенно);
          2) SQLite (если передан db) — для повторов между каналами и
             после рестарта.
        """
        fp = _fingerprint(text)
        now = time.monotonic()

        # --- слой 1: in-memory ---
        # Ленивая сборка мусора: выкинем всё, что старше ttl.
        expired = [key for key, ts in self._seen.items() if now - ts > self._ttl]
        for key in expired:
            del self._seen[key]

        if fp in self._seen:
            # Обновим временную метку — «продлеваем» окно для активных дублей.
            self._seen[fp] = now
            return True

        # --- слой 2: SQLite (персистентный) ---
        if self._db is not None:
            if self._db.is_duplicate_persistent(fp):
                # Запомним и в памяти, чтобы следующий раз не лезть в БД.
                self._seen[fp] = now
                return True

        self._seen[fp] = now
        return False
