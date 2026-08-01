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
    """Простой TTL-кеш «видели ли мы уже такое сообщение».

    Не thread-safe, но безопасен в рамках одного asyncio event loop,
    чего достаточно для данного приложения.
    """

    def __init__(self, ttl: int) -> None:
        # ttl в секундах; запись хранит время последнего «увиденного» момента.
        self._ttl = ttl
        self._seen: dict[str, float] = {}

    def is_duplicate(self, text: str) -> bool:
        """Вернуть True, если такое сообщение уже приходило за окно TTL.

        Побочно подчищает протухшие записи, чтобы кеш не рос бесконечно.
        """
        fp = _fingerprint(text)
        now = time.monotonic()

        # Ленивая сборка мусора: выкинем всё, что старше ttl.
        expired = [key for key, ts in self._seen.items() if now - ts > self._ttl]
        for key in expired:
            del self._seen[key]

        if fp in self._seen:
            # Обновим временную метку — «продлеваем» окно для активных дублей.
            self._seen[fp] = now
            return True

        self._seen[fp] = now
        return False
