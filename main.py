"""main.py — точка входа AirRadar AI.

Оркестрация:
    Telethon NewMessage (исходные каналы)
      -> fast_filter (быстрый отсев по ключевым словам)
      -> dedup (игнор повторов внутри TTL-окна)
      -> ai_summarizer (Ollama; fallback на оригинал при ошибке)
      -> sticker.get_sticker_header (эмодзи-заголовок по типу угрозы)
      -> publisher (Bot API -> целевой канал)

Дополнительно:
  * единая aiohttp.ClientSession для summarizer'а и publisher'а;
  * фоновый healthcheck Ollama;
  * структурированное логирование с уровнем из .env;
  * graceful shutdown по Ctrl+C / SIGTERM (закрытие сессий и Telethon-клиента).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import aiohttp
from telethon import TelegramClient, events
from telethon.tl.custom import Message

import config
from ai_summarizer import AISummarizer
from dedup import DedupCache
from fast_filter import matches_keywords
from publisher import Publisher
from sticker import get_sticker_header

logger = logging.getLogger("airradar")


def setup_logging(level: str) -> None:
    """Настроить logging единым форматом для всего приложения."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Telethon очень разговорчив на DEBUG — приглушим до WARNING, если не debug.
    if level != "DEBUG":
        logging.getLogger("telethon").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run() -> None:
    """Главная асинхронная точка: инициализация всего и запуск Telethon."""
    settings = config.load_settings()
    setup_logging(settings.log_level)

    logger.info("AirRadar AI запускается…")
    logger.info("Исходных каналов: %d", len(settings.source_channels))
    logger.info("Целевой канал: %s", settings.target_channel)
    logger.info("Ollama: %s (model=%s)", settings.ollama_url, settings.ollama_model)

    # --- единый HTTP-слой на всё приложение (Ollama + Bot API) ---
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.http_timeout)
    )

    summarizer = AISummarizer(
        base_url=settings.ollama_url,
        model=settings.ollama_model,
        timeout=settings.http_timeout,
        session=http_session,
    )
    publisher = Publisher(
        bot_token=settings.bot_token,
        target_channel=settings.target_channel,
        timeout=settings.http_timeout,
        session=http_session,
    )
    dedup = DedupCache(ttl=settings.dedup_ttl)

    # --- Telethon-клиент (User API) для чтения исходных каналов ---
    client = TelegramClient(
        settings.session_name,
        settings.tg_api_id,
        settings.tg_api_hash,
        connection_retries=None,  # бесконечные попытки реконнекта (встроено)
        retry_delay=2,
        auto_reconnect=True,
    )

    # --- запуск и резолв каналов через обход диалогов ---
    # Telethon резолвит каналы в NewMessage через кеш сущностей. Приватные
    # каналы (числовой -100 ID) туда не попадают по строке — нужно получить их
    # entity напрямую из диалогов. Поэтому: подключаемся, обходим диалоги и
    # собираем готовые сущности для каждого искомого канала. Эти сущности и
    # передаём в NewMessage(chats=...) — это самый надёжный способ.
    await client.start()
    logger.info("Загружаю диалоги и резолвлю каналы…")

    # Нормализуем искомые ID: '@username' оставляем как есть, а '-100NNN'
    # превращаем в число NNN для сравнения с dialog.entity.id.
    target_ids: dict[int, str] = {}
    target_usernames: dict[str, str] = {}
    for ch in settings.source_channels:
        ch_clean = ch.strip().lstrip("@").lower()
        if ch_clean.startswith("-100") and ch_clean[4:].isdigit():
            target_ids[int(ch_clean[4:])] = ch
        elif ch.startswith("@"):
            target_usernames[ch_clean] = ch
        else:
            # числовой ID без префикса — трактуем как raw id канала
            if ch_clean.lstrip("-").isdigit():
                target_ids[int(ch_clean.lstrip("-"))] = ch

    resolved_chats: list = []
    resolved_titles: list[str] = []
    missed: list[str] = list(settings.source_channels)  # копия для вычёркивания

    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        eid = getattr(ent, "id", None)
        uname = (getattr(ent, "username", None) or "").lower()
        match_key = None
        if eid is not None and eid in target_ids:
            match_key = target_ids[eid]
        elif uname and uname in target_usernames:
            match_key = target_usernames[uname]
        if match_key is not None:
            resolved_chats.append(ent)
            resolved_titles.append(getattr(ent, "title", None) or match_key)
            if match_key in missed:
                missed.remove(match_key)

    logger.info(
        "Доступно каналов: %d/%d | %s",
        len(resolved_chats),
        len(settings.source_channels),
        ", ".join(resolved_titles) or "—",
    )
    if missed:
        logger.warning("Недоступные каналы: %s", ", ".join(missed))

    if not resolved_chats:
        raise RuntimeError(
            "Ни один исходный канал не доступен аккаунтом. "
            "Проверь SOURCE_CHANNELS в .env и подписки аккаунта."
        )

    # Обработчик новых сообщений из всех исходных каналов.
    @client.on(events.NewMessage(chats=resolved_chats))
    async def handler(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        await _process_message(event, summarizer, publisher, dedup)

    # --- фоновый healthcheck Ollama ---
    health_task = asyncio.create_task(
        _healthcheck_loop(summarizer, settings.healthcheck_interval),
        name="ollama-healthcheck",
    )

    # --- graceful shutdown ---
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Получен сигнал завершения — останавливаемся…")
        stop_event.set()

    # Windows: SIGTERM может отсутствовать; защищаемся try/except.
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                # Windows не поддерживает add_signal_handler для SIGTERM —
                # полагаемся на KeyboardInterrupt ниже.
                pass

    me = await client.get_me()
    logger.info("Подключено как @%s (id=%s). Слушаю каналы…", me.username, me.id)

    # Боты не могут читать чужие каналы — только User-аккаунт. Если вошли
    # через bot token, явно предупредим, чтобы не гадать, почему нет сообщений.
    if me.bot:
        logger.error(
            "Вход выполнен через BOT TOKEN. Боты НЕ могут читать каналы — "
            "сообщения поступать не будут. Перезапусти и на вопрос "
            "'Please enter your phone' введи свой НОМЕР ТЕЛЕФОНА (например +380...)."
        )

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Ctrl+C — выходим.")
    finally:
        # Закрываем всё даже при жестком выходе — чтобы не было
        # предупреждений "Unclosed client session".
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass

        logger.info("Отключаю Telethon и закрываю сессии…")
        await client.disconnect()
        await summarizer.aclose()
        await publisher.aclose()
        await http_session.close()
        logger.info("AirRadar AI остановлен.")


async def _process_message(
    event: events.NewMessage.Event,  # noqa: ANN001
    summarizer: AISummarizer,
    publisher: Publisher,
    dedup: DedupCache,
) -> None:
    """Полный конвейер обработки одного входящего сообщения.

    Любые исключения на отдельных этапах логируются и НЕ роняют обработчик,
    чтобы одно «плохое» сообщение не убило чтение из всех каналов.
    """
    message: Message = event.message
    text = (message.text or message.message or "").strip()
    if not text:
        return  # медиа без текста — пропускаем

    # 1) Быстрый фильтр по ключевым словам (до тяжёлого ИИ).
    if not matches_keywords(text):
        return

    # 2) Дедупликация — повторы внутри TTL-окна игнорируем.
    if dedup.is_duplicate(text):
        logger.debug("Дубликат пропущен: %s", text[:60])
        return

    source = getattr(event.chat, "username", None) or getattr(event.chat, "id", "?")
    logger.info("Новое сообщение от %s: %s", source, text[:80])

    try:
        # 3) Сжатие через Ollama (fallback на оригинал — внутри summarizer).
        summary = await summarizer.summarize(text)
        # 4) Эмодзи-заголовок по типу угрозы.
        header = get_sticker_header(text)
        # 5) Сборка и публикация.
        final_text = f"{header}\n{summary}".strip()
        await publisher.send(final_text)
    except Exception as exc:  # pragma: no cover — страховка конвейера
        logger.exception("Сбой обработки сообщения (пропускаем): %s", exc)


async def _healthcheck_loop(summarizer: AISummarizer, interval: int) -> None:
    """Периодический пинг Ollama — раннее обнаружение падения локального ИИ."""
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await summarizer.healthcheck()
            if ok:
                logger.debug("Healthcheck Ollama: OK")
            else:
                logger.warning("Healthcheck Ollama: НЕДОСТУПНА — работает fallback на оригинал.")
    except asyncio.CancelledError:
        # Нормальный выход при остановке приложения.
        raise


def main() -> None:
    """Синхронная обёртка для запуска через ``python main.py``."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # Уже обработано внутри run(), здесь просто не печатаем traceback.
        pass
    except RuntimeError as exc:
        # Частый случай: ошибки конфигурации (нет .env и т.п.).
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
