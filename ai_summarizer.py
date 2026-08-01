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


# ============================================================
#  Groq Cloud API — облачная альтернатива Ollama (для VPS 24/7).
#  Использует OpenAI-совместимый /openai/v1/chat/completions.
#  Та же семантика fallback: при ошибке возвращает оригинал.
# ============================================================

# Минимальный «интерфейс» summarizer-а: и Ollama, и Groq реализуют его.
class SummarizerProtocol:
    """Условный интерфейс: summarize(text) -> str, healthcheck() -> bool."""

    async def summarize(self, text: str) -> str:  # pragma: no cover
        raise NotImplementedError

    async def healthcheck(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover
        pass


class GroqSummarizer(SummarizerProtocol):
    """Сжатие через Groq Cloud API (OpenAI-совместимый chat completions).

    Идеально для VPS 24/7: не требует RAM под модель (вся инференция в облаке),
    отвечает за ~0.2–0.5 сек. Бесплатный tier: ~14 400 запросов/день.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int,
        base_url: str = "https://api.groq.com/openai",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def summarize(self, text: str) -> str:
        """Сжать текст через Groq. При ошибке — fallback на оригинал."""
        text = text.strip()
        if not text:
            return text

        # OpenAI-совместимый формат: system prompt + user (исходный текст).
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,   # минимум фантазии — только факты
            "max_tokens": 80,     # жёсткий лимит на короткую выжимку
            "stream": False,
        }

        try:
            session = await self._get_session()
            async with session.post(self._url, json=payload, headers=self._headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Groq HTTP %s, fallback на оригинал. body=%s",
                        resp.status, body[:300],
                    )
                    return text

                data = await resp.json()
                # Стандартный OpenAI-формат ответа.
                choices = data.get("choices") or []
                summary = ""
                if choices:
                    summary = (choices[0].get("message", {}).get("content") or "").strip()
                if not summary:
                    logger.warning("Groq вернул пустой ответ, fallback на оригинал.")
                    return text

                logger.debug("Groq OK: %d -> %d chars", len(text), len(summary))
                return summary

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Groq недоступен (%s), fallback на оригинал.", exc)
            return text
        except Exception as exc:
            logger.exception("Неожиданная ошибка Groq, fallback на оригинал: %s", exc)
            return text

    async def healthcheck(self) -> bool:
        """Проверка доступности Groq лёгким запросом к /models (GET, без токенов)."""
        try:
            session = await self._get_session()
            models_url = self._url.rsplit("/", 1)[0] + "/models"
            async with session.get(models_url, headers=self._headers) as resp:
                ok = resp.status == 200
                if not ok:
                    logger.warning("Healthcheck Groq: HTTP %s", resp.status)
                return ok
        except aiohttp.ClientError as exc:
            logger.warning("Healthcheck Groq: сеть недоступна (%s)", exc)
            return False
        except Exception as exc:  # pragma: no cover
            logger.warning("Healthcheck Groq: неожиданная ошибка (%s)", exc)
            return False


def make_summarizer(backend: str, settings: Any, session: aiohttp.ClientSession) -> SummarizerProtocol:
    """Фабрика summarizer-а по настройкам.

    backend == 'ollama' → локальная Ollama (тяжёлая, нужна RAM под модель).
    backend == 'groq'   → облачный Groq API (лёгкий, для VPS 24/7).
    Любое другое значение → ошибка с понятным сообщением.
    """
    backend = (backend or "").strip().lower()
    if backend == "ollama":
        return AISummarizer(
            base_url=settings.ollama_url,
            model=settings.ollama_model,
            timeout=settings.http_timeout,
            session=session,
        )
    if backend == "groq":
        if not settings.groq_api_key:
            raise RuntimeError(
                "LLM_BACKEND=groq, но GROQ_API_KEY пуст. Получи ключ на "
                "https://console.groq.com/keys и впиши в .env."
            )
        return GroqSummarizer(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            timeout=settings.http_timeout,
            base_url=settings.groq_url,
            session=session,
        )
    raise RuntimeError(
        f"Неизвестный LLM_BACKEND={backend!r}. Допустимо: 'ollama' или 'groq'."
    )
