"""ai_summarizer.py — сжатие сообщений локальной Ollama (модель qwen2.5:3b).

Содержит:
  * SYSTEM_PROMPT — системный промпт для Qwen 2.5 (промпт 2 из ТЗ), дословно.
  * AISummarizer — async-обёртка над ``POST {OLLAMA_URL}/api/generate``.
    При любой ошибке Ollama (недоступна, таймаут, кривой ответ) возвращает
    исходный текст — скрипт не падает, публикация продолжается (по ТЗ).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Системный промпт для Qwen 2.5 — «военный оперативный аналитик».
# Текст дословно по ТЗ; модель получает лишь сухую выжимку без эмодзи и воды.
SYSTEM_PROMPT = """\
Ты — военный оперативный аналитик. Твоя задача — мгновенно сокращать входящие \
сообщения о воздушных и военных угрозах.

Правила обработки:
1. Выдели ТОЛЬКО сухие факты: Тип объекта (БПЛА, ракета, КАБ), \
Направление/Локация, Текущий статус (летит, взрыв, отбой).
2. Максимальная длина ответа: 1–2 предложения (до 20–25 слов).
3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО: добавлять вводные слова («По сообщению...», \
«Внимание...»), использовать эмодзи (их добавит бот), оставлять ссылки, \
рекламу, призывы подписываться и эмоции.
4. Ответ должен содержать ТОЛЬКО итоговый сжатый текст и ничего больше.

Пример входного текста:
"Ребята, внимание! Зафиксирован пролет группы вражеских БПЛА типа Шахед \
со стороны Сумской области в направлении Полтавщины! Не игнорируйте тревогу, \
подписывайтесь на наш канал!"
Пример твоего ответа:
"Группа БПЛА (Шахед) движется из Сумской области в направлении Полтавской."\
"""


class AISummarizer:
    """Async-клиент локальной Ollama с отказоустойчивым fallback."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._generate_url = f"{base_url.rstrip('/')}/api/generate"
        self._tags_url = f"{base_url.rstrip('/')}/api/tags"
        self._model = model
        # timeout=total; Ollama может «думать» дольше обычного HTTP — держим запас.
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Лениво создать сессию, если её не передали снаружи."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        """Закрыть внутреннюю сессию, если summarizer владеет ею."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def summarize(self, text: str) -> str:
        """Сжать текст через Ollama.

        Возвращает сжатый текст. При любой ошибке (сеть, таймаут, некорректный
        JSON, пустой ответ модели) логирует warning и возвращает ИСХОДНЫЙ текст,
        чтобы публикация не прерывалась.
        """
        text = text.strip()
        if not text:
            return text

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": text,
            "system": SYSTEM_PROMPT,
            "stream": False,  # один цельный ответ — проще и надёжнее парсить
            "options": {
                "temperature": 0.2,  # минимум фантазии: нужны факты, а не «творчество»
                "num_predict": 80,   # жёсткий лимит токенов на короткую выжимку
            },
        }

        try:
            session = await self._get_session()
            async with session.post(self._generate_url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Ollama вернула HTTP %s, fallback на оригинал. body=%s",
                        resp.status,
                        body[:300],
                    )
                    return text

                data = await resp.json()
                summary = (data.get("response") or "").strip()
                if not summary:
                    logger.warning("Ollama вернула пустой ответ, fallback на оригинал.")
                    return text

                logger.debug("Ollama OK: %d -> %d chars", len(text), len(summary))
                return summary

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            # Сеть/таймаут/битый JSON — публикуем оригинал, скрипт не падает.
            logger.warning("Ollama недоступна (%s), fallback на оригинал.", exc)
            return text
        except Exception as exc:  # прочие неожиданные ошибки — не роняем конвейер
            logger.exception("Неожиданная ошибка Ollama, fallback на оригинал: %s", exc)
            return text

    async def healthcheck(self) -> bool:
        """Быстрая проверка доступности Ollama через ``GET /api/tags``."""
        try:
            session = await self._get_session()
            async with session.get(self._tags_url) as resp:
                ok = resp.status == 200
                if not ok:
                    logger.warning("Healthcheck Ollama: HTTP %s", resp.status)
                return ok
        except aiohttp.ClientError as exc:
            logger.warning("Healthcheck Ollama: сеть недоступна (%s)", exc)
            return False
        except Exception as exc:  # pragma: no cover
            logger.warning("Healthcheck Ollama: неожиданная ошибка (%s)", exc)
            return False
