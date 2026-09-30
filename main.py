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
import time

import aiohttp
from telethon import TelegramClient, events
from telethon.tl.custom import Message

import config
from aggregator import AlertAggregator, PendingAlert
from ai_summarizer import SummarizerProtocol, is_ignored_summary, make_summarizer
from alert_renderer import render_evidence_alert
from bot_ui import register_handlers
from database import Database
from dedup import DedupCache
from fast_filter import clean_signature, matches_keywords
from health_server import start_health_server
from publisher import Publisher
from regions import detect_kyiv_zone, detect_region, region_name
from incident_fusion import extract_incident_fact
from source_policy import normalize_source, source_group
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

    # --- единый HTTP-слой на всё приложение (LLM + Bot API) ---
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.http_timeout)
    )

    # Фабрика выбирает бэкенд ИИ по LLM_BACKEND: 'ollama' (локально) или
    # 'groq' (облако, для VPS 24/7). Оба используют один системный промпт.
    summarizer = make_summarizer(settings.llm_backend, settings, http_session)
    logger.info(
        "LLM-бэкенд: %s (%s)",
        settings.llm_backend,
        settings.groq_model if settings.llm_backend == "groq" else settings.ollama_model,
    )

    # --- SQLite-журнал угроз/тревог (фундамент статистики и ETA) ---
    db = Database(settings.db_path)
    logger.info("БД журнала: %s", settings.db_path)

    # HTTP-сервер: health-check + API для карты угроз (/api/threats).
    # set_db передаёт Database, чтобы API могло отдавать активные угрозы.
    from health_server import set_db
    set_db(db)
    _health_task = await start_health_server()
    publisher = Publisher(
        bot_token=settings.bot_token,
        target_channel=settings.target_channel,
        timeout=settings.http_timeout,
        session=http_session,
        promo_url=settings.promo_channel_url,
    )
    # Кнопка «Поділитися» ведёт на наш канал (та же ссылка, что в постах).
    global PROMO_URL
    PROMO_URL = settings.promo_channel_url or PROMO_URL
    # Сохраняем ссылку для /summary (ручной запуск сводки из admin_ui).
    global _publisher_ref
    _publisher_ref = publisher

    # Дедупликация: гибрид in-memory + SQLite. Переживает рестарт и ловит
    # поздние репосты между каналами (окно в БД — до 1 часа).
    dedup = DedupCache(ttl=settings.dedup_ttl, db=db)

    # Агрегатор: сообщения одного инцидента в окне AGGREGATE_WINDOW_SEC
    # уходят в канал одним постом (критичные/отбой — мгновенно, байпасом).
    aggregator = AlertAggregator(
        settings.aggregate_window_sec,
        lambda items: _publish_items(db, publisher, bot_client, items),
    )

    # --- второй клиент: бот @AirRadar_AI_bot (Bot API) для интерактивного меню ---
    # Читает /start и нажатия inline-кнопок в личке. Публикацией в канал
    # по-прежнему занимается Publisher (HTTP), а здесь — приём команд.
    bot_client: TelegramClient | None = None
    if settings.bot_token and settings.admin_id:
        bot_client = TelegramClient(
            f"{settings.session_name}_bot",
            settings.tg_api_id,
            settings.tg_api_hash,
        )
        register_handlers(bot_client, db, settings.admin_id, settings.webapp_url, settings.map_webapp_url)
        # Interests-модуль: команды /add_channel, /my_channels + кнопки тем.
        from interests_ui import register_interests_handlers
        register_interests_handlers(bot_client, db, settings.admin_id)
        # Админка (5.4): /status, /give_admin, /ban_channel.
        from admin_ui import register_admin_handlers
        register_admin_handlers(bot_client, db, settings.admin_id)
        # Mini App: приём данных из WebApp (set_topic/add_channel/remove_channel).
        from miniapp_handler import register_miniapp_handlers
        register_miniapp_handlers(bot_client, db, settings.admin_id)
        # Репорты угроз с геопозицией (/report + приём локации в ЛС).
        from geo_report import register_geo_report_handlers
        register_geo_report_handlers(bot_client, db)
        if settings.webapp_url:
            logger.info("Mini App включён: %s", settings.webapp_url)
        logger.info(
            "Меню бота включено (admin_id=%s): Military + Interests. Напиши /start.",
            settings.admin_id,
        )
    else:
        logger.info("Меню бота отключено (ADMIN_ID не задан) — только публикация.")

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

    # Обработчик новых сообщений из всех исходных (военных) каналов.
    @client.on(events.NewMessage(chats=resolved_chats))
    async def handler(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        await _process_message(
            event, summarizer, publisher, dedup, db, bot_client,
            http_session, settings.groq_api_key, settings.groq_vision_model, settings,
            aggregator,
        )

    # --- Interests-модуль: обработчик постов из user-каналов ---
    # Каналы добавляются пользователями через /add_channel и должны начать
    # парситься БЕЗ рестарта. Поэтому: один обработчик на все сообщения с
    # дешёвым фильтром по числовому id + фоновая задача, раз в минуту
    # подтягивающая новые каналы из БД (обход диалогов).
    from interests_handler import process_interests_message

    interests_ids: set[int] = set()        # «голые» id чатов user-каналов
    interests_resolved_keys: set[str] = set()  # ключи каналов, уже найденные в диалогах

    def _channel_key(raw: str) -> str:
        """Нормализовать '@Name' / 'name' / '-100123' / '123' к ключу сверки."""
        v = (raw or "").strip().lower().lstrip("@")
        if v.startswith("-100") and v[4:].isdigit():
            return v[4:]
        if v.startswith("-") and v[1:].isdigit():
            return v[1:]
        return v

    async def _resolve_new_interests_channels() -> None:
        """Найти в диалогах user-каналы, добавленные с прошлого обхода."""
        wanted = {_channel_key(ch) for ch in db.all_interests_channels()}
        missing = wanted - interests_resolved_keys
        if not missing:
            return
        found: dict[str, int] = {}
        async for dialog in client.iter_dialogs():
            if not missing:
                break
            ent = dialog.entity
            uname = (getattr(ent, "username", None) or "").lower()
            eid = getattr(ent, "id", None)
            for ch in list(missing):
                if (uname and uname == ch) or (eid is not None and str(eid) == ch):
                    found[ch] = int(eid)
                    missing.discard(ch)
        for ch, eid in found.items():
            interests_resolved_keys.add(ch)
            if eid not in interests_ids:
                interests_ids.add(eid)
                logger.info("Interests: подключён user-канал id=%s", eid)
        if missing:
            logger.info(
                "Interests: жду подписки аккаунта на каналы: %s",
                ", ".join(sorted(missing)),
            )

    async def _interests_refresh_loop(interval: int = 60) -> None:
        """Раз в минуту подтягивать новые user-каналы без рестарта."""
        while True:
            await asyncio.sleep(interval)
            try:
                await _resolve_new_interests_channels()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                logger.exception("Сбой обновления user-каналов Interests")

    @client.on(events.NewMessage())
    async def interests_handler(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        cid = event.chat_id
        if cid is None:
            return
        if cid < 0:
            # Маркированный id (-100…) → «голый» id канала для сверки.
            s = str(cid)
            cid = int(s[4:]) if s.startswith("-100") else int(s[1:])
        if cid not in interests_ids:
            return
        await process_interests_message(
            event, summarizer, dedup, db, bot_client,
            http_session, settings.groq_api_key, settings.groq_vision_model,
        )

    # Первичный резолв user-каналов + фоновая синхронизация новых (/add_channel).
    await _resolve_new_interests_channels()
    if interests_ids:
        logger.info("Interests: подключено user-каналов: %d", len(interests_ids))
    interests_task = asyncio.create_task(
        _interests_refresh_loop(), name="interests-refresh"
    )

    # --- фоновый healthcheck Ollama ---
    health_task = asyncio.create_task(
        _healthcheck_loop(summarizer, settings.healthcheck_interval),
        name="ollama-healthcheck",
    )

    # --- планировщик дайджестов (утро/вечер) ---
    digest_task = None
    if bot_client is not None:
        from digest import digest_scheduler
        digest_task = asyncio.create_task(
            digest_scheduler(bot_client, db,
                             settings.digest_morning_hour, settings.digest_evening_hour,
                             summarizer, publisher),
            name="digest-scheduler",
        )

    # --- проактивные предупреждения «можлива нова тривога» (статистика волн) ---
    pre_wave_task = None
    if bot_client is not None and settings.pre_wave_notice:
        pre_wave_task = asyncio.create_task(
            _pre_wave_loop(bot_client, db),
            name="pre-wave-notice",
        )
        logger.info("Предупреждения «можлива нова тривога» включены (статистика волн).")

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

    # Стартуем бота-меню в фоне (неблокирующе). Он принимает /start и кнопки.
    if bot_client is not None:
        await bot_client.start(bot_token=settings.bot_token)
        bot_me = await bot_client.get_me()
        logger.info("Бот меню подключён как @%s. Напиши ему /start в личку.", bot_me.username)

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
        if digest_task is not None:
            digest_task.cancel()
            try:
                await digest_task
            except asyncio.CancelledError:
                pass
        if pre_wave_task is not None:
            pre_wave_task.cancel()
            try:
                await pre_wave_task
            except asyncio.CancelledError:
                pass
        interests_task.cancel()
        try:
            await interests_task
        except asyncio.CancelledError:
            pass

        logger.info("Отключаю Telethon и закрываю сессии…")
        if bot_client is not None:
            await bot_client.disconnect()
        await client.disconnect()
        await aggregator.aclose()
        await summarizer.aclose()
        await publisher.aclose()
        await http_session.close()
        db.close()
        logger.info("AirRadar AI остановлен.")


async def _process_message(
    event: events.NewMessage.Event,  # noqa: ANN001
    summarizer: SummarizerProtocol,
    publisher: Publisher,
    dedup: DedupCache,
    db: Database,
    bot_client=None,
    http_session=None,
    groq_api_key: str = "",
    vision_model: str = "",
    settings=None,
    aggregator: AlertAggregator | None = None,
) -> None:
    """Полный конвейер обработки одного входящего сообщения.

    Любые исключения на отдельных этапах логируются и НЕ роняют обработчик,
    чтобы одно «плохое» сообщение не убило чтение из всех каналов.
    bot_client: TelegramClient бота — нужен для рассылки по подпискам (если None,
    рассылка пропускается).
    """
    message: Message = event.message
    text = (message.text or message.message or "").strip()
    # Если есть фото — распознаём текст с него (OCR, ТЗ 5.1) и объединяем с подписью.
    if getattr(message, "photo", None) is not None and http_session is not None and groq_api_key and vision_model.strip():
        from media_ocr import extract_text_with_ocr
        text = await extract_text_with_ocr(
            event, http_session, groq_api_key, vision_model, timeout=settings.http_timeout
        )
    if not text:
        return  # медиа без текста и без распознанного — пропускаем

    # 0) Очистка подписи канала ДО всех проверок — хвост «➡️Підписатись»
    #    засоряет фильтр, дедупликацию и определение региона.
    text = clean_signature(text)

    # 1) Быстрый фильтр по ключевым словам (до тяжёлого ИИ).
    if not matches_keywords(text):
        return

    # 2) Дедупликация — повторы внутри TTL-окна игнорируем.
    if dedup.is_duplicate(text):
        logger.debug("Дубликат пропущен: %s", text[:60])
        return

    source = normalize_source(str(getattr(event.chat, "username", None) or getattr(event.chat, "id", "?")))
    if db.is_channel_disabled(source, "military"):
        logger.info("Отключённый источник пропущен: %s", source)
        return
    logger.info("Новое сообщение от %s: %s", source, text[:80])
    # Отмечаем канал живым (для /status админки).
    db.channel_seen(source, "military")

    try:
        # 3) Classify before LLM: ordinary local news must never become a
        # fabricated weapon alert because a context-only filter matched it.
        from analytics import _detect_stage
        from weapon_classes import classify_weapon
        weapon = classify_weapon(text)
        stage = _detect_stage(text, weapon)
        event_ts = int(getattr(message, "date", None).timestamp()) if getattr(message, "date", None) else int(time.time())
        if weapon == "unknown":
            logger.info("Неклассифицированный пост пропущен: %s", text[:80])
            db.record_inference_audit(event_ts=event_ts, source=source, model=getattr(summarizer, "_model", "unknown"), raw_output="", decision="rejected", reason="unknown_weapon")
            return
        # 4) Сжатие через LLM только для уже распознанной угрозы.
        summary = await summarizer.summarize(text)
        if is_ignored_summary(summary):
            db.record_inference_audit(event_ts=event_ts, source=source, model=getattr(summarizer, "_model", "unknown"), raw_output=summary, decision="ignored")
            logger.info("Неподтверждённый звуковой пост пропущен: %s", text[:80])
            return
        db.record_inference_audit(event_ts=event_ts, source=source, model=getattr(summarizer, "_model", "unknown"), raw_output=summary, decision="accepted")
        # 5) One canonical class for publication, storage and forecasts.
        # Старый журнал ожидает ограниченный набор типов. Маппинг от
        # канонического класса не даёт двум разным классификаторам спорить.
        threat_type = {
            "stand_down": "stand_down", "shahed": "uav", "uav": "uav",
            "fpv": "uav", "recon_drone": "uav", "explosion": "explosion",
            "air_defense": "explosion", "mlrs": "artillery", "artillery": "artillery",
        }.get(weapon, "missile" if weapon not in {"unknown", "alert", "decoy"} else "other")
        regions = detect_region(text, channel=source)
        fact = extract_incident_fact(text, weapon, stage)
        group = source_group(source, getattr(settings, "source_groups", {}))
        # Fusion owns the target location: in "from Sumy to Kyiv" Kyiv is the incident.
        confirmation = db.merge_incident_fact(
            event_ts=event_ts, source=source, source_group=group, fact=fact, text=text,
            window_seconds=getattr(settings, "incident_window_seconds", 1200),
            confirmation_sources=getattr(settings, "confirmation_sources", 2),
        )
        # 5) Publish active recognised threats immediately; a single source is
        # allowed and clearly labelled in the alert. Fast monitoring posts
        # like «3 БпЛА на Путивль» have no motion verb, so stage "unknown"
        # is still actionable. Predictive or generic aviation noise stays
        # internal, while all-clear must always reach the channel.
        public_class = weapon not in {"aviation", "tac_aviation", "strat_aviation", "alert"}
        # IN_FLIGHT_ONLY (по умолчанию): в канал летит только то, что уже в
        # воздухе (imminent) либо отбой. Стадия unknown («3 БпЛА на Путивль»
        # без глагола движения) пишется в БД/карту, но не публикуется —
        # половина таких постов оказывается рутиной/устаревшей сводкой.
        unknown_ok = not getattr(settings, "in_flight_only", True)
        publishable = weapon == "stand_down" or (
            (stage == "imminent" or (stage == "unknown" and unknown_ok)) and public_class
        )
        # Непубликуемое (последствия удара, ППО, авиация) НЕ прерывает
        # конвейер ранним return: журнал БД ниже обязан зафиксировать
        # impact-событие, иначе ETA и статистика региона не получают
        # пары пуск→прилёт. Публикация при этом по-прежнему закрыта.
        if not publishable:
            logger.info("Неподтверждённый/потенциальный пост не опубликован: %s", text[:80])
        elif aggregator is not None:
            await aggregator.submit(
                PendingAlert(
                    text=text, source=source, event_ts=event_ts, fact=fact,
                    confirmation=confirmation, regions=regions,
                )
            )
        else:
            # Fallback без агрегатора (прямые вызовы в тестах): прежнее поведение.
            final_text = render_evidence_alert(
                text=text, source=source, event_ts=event_ts, fact=fact, confirmation=confirmation
            )
            publication = db.incident_publication(confirmation.get("key", ""))
            if publication and confirmation.get("material_update"):
                published = await publisher.edit(str(publication["published_chat_id"]), int(publication["published_message_id"]), final_text)
            else:
                published = await publisher.send(final_text)
                if published and confirmation.get("key"):
                    chat = published.get("chat", {})
                    db.save_incident_publication(confirmation["key"], str(chat.get("id", "")), int(published.get("message_id", 0)))
            if not published:
                logger.warning("Пост не записан как опубликованный: отправка в канал не удалась")
            if bot_client is not None and regions and published:
                fkey = confirmation.get("key", "")
                await _notify_subscribers(
                    bot_client, db, _notify_regions(fact, regions), final_text,
                    feedback_key=fkey, weapon_class=weapon,
                )

        # 6) Определение регионов и запись в журнал БД.
        regions_to_log = regions or ["unknown"]
        if weapon == "stand_down":
            event_stage, outcome = "all_clear", "unknown"
        elif stage == "past":
            event_stage, outcome = "impact", "impact"
        elif weapon == "air_defense":
            event_stage, outcome = "intercept", "intercept"
        elif weapon == "alert":
            event_stage, outcome = "alert", "unknown"
        else:
            event_stage, outcome = ("launch" if "пуск" in text.lower() else "movement"), "unknown"
        for slug in regions_to_log:
            db.add_event(event_ts=event_ts, weapon_class=weapon, stage=event_stage, region=slug,
                         text=text, source=source, outcome=outcome, confidence=0.8)
            db.add_threat(
                threat_type=threat_type,
                region=slug,
                text=summary,
                source=str(source),
                event_ts=event_ts,
            )
        # 6б) Извлечение сущностей (город/объект/последствия/оружие/ППО) —
        #     для детальной статистики. Только не-отбой.
        if threat_type != "stand_down" and regions:
            from ai_summarizer import extract_entities
            entities = await extract_entities(summarizer, text, regions)
            if entities:
                for slug in regions:
                    db.save_entities(slug, entities)

        # Тревоги открывают лишь явная сирена/активная угроза, а не последствия
        # удара, ППО или общая оперативная заметка.
        if weapon == "stand_down" or threat_type == "stand_down":
            # Отбой остаётся сообщением источника: закрывает локально созданный
            # статус, но UI не интерпретирует это как независимое «тихо».
            for slug in regions or []:
                db.alert_end(slug, event_ts=event_ts)
        elif confirmation.get("status") in ("corroborated", "officially_confirmed") and stage == "imminent":
            for slug in regions or []:
                db.alert_start(slug, event_ts=event_ts)
                # Метрика «випередження сирени»: первый наш пост по региону
                # в предшествующие 2 часа против старта этого эпизода тревоги.
                try:
                    first = db._conn.execute(
                        "SELECT MIN(event_ts) AS first_ts FROM threat_events "
                        "WHERE region = ? AND stage IN ('launch', 'movement', 'alert') AND event_ts >= ?",
                        (slug, event_ts - 7200),
                    ).fetchone()
                    if first and first["first_ts"] is not None:
                        db.record_siren_lead(slug, int(first["first_ts"]), event_ts)
                except Exception:  # noqa: BLE001 — метрика не должна ронять конвейер
                    logger.debug("siren_lead: не удалось записать замер (%s)", slug)
        # Рассылка по подпискам выполняется в _publish_items после публикации
        # (агрегированный пост — одна рассылка по объединённым регионам).
    except Exception as exc:  # pragma: no cover — страховка конвейера
        logger.exception("Сбой обработки сообщения (пропускаем): %s", exc)
        db.channel_error(str(source), "military", str(exc))


async def _publish_items(db: Database, publisher: Publisher, bot_client, items: list) -> None:
    """Опубликовать набор агрегированных сообщений одним постом.

    Рендер идёт от последнего элемента (самое свежее состояние инцидента),
    источники собираются уникальные по порядку поступления. Если инцидент уже
    публиковался — обновляем существующий пост (edit), иначе отправляем новый
    и сохраняем message_id для всех ключей набора. Затем — одна рассылка
    подписчикам по объединённым регионам.
    """
    if not items:
        return
    last = items[-1]
    sources: list[str] = []
    for item in items:
        if item.source not in sources:
            sources.append(item.source)
    final_text = render_evidence_alert(
        text=last.text, source=last.source, sources=sources,
        event_ts=last.event_ts, fact=last.fact, confirmation=last.confirmation,
    )
    # Уже публиковали один из инцидентов набора? Перебираем ключи с конца.
    publication = None
    for item in reversed(items):
        key = item.confirmation.get("key", "")
        if key:
            publication = db.incident_publication(key)
            if publication:
                break
    if publication and last.confirmation.get("material_update"):
        published = await publisher.edit(
            str(publication["published_chat_id"]), int(publication["published_message_id"]), final_text
        )
    else:
        published = await publisher.send(final_text)
        if published:
            chat = published.get("chat", {})
            for item in items:
                key = item.confirmation.get("key", "")
                if key:
                    db.save_incident_publication(
                        key, str(chat.get("id", "")), int(published.get("message_id", 0))
                    )
    if not published:
        logger.warning("Пост не записан как опубликованный: отправка в канал не удалась")
        return
    regions = _notify_regions(last.fact, [
        slug for item in items for slug in item.regions if slug
    ])
    if bot_client is not None and regions:
        fkey = ""
        for item in reversed(items):
            fkey = item.confirmation.get("key", "")
            if fkey:
                break
        await _notify_subscribers(
            bot_client, db, regions, final_text,
            feedback_key=fkey, weapon_class=last.fact.weapon_class,
        )


# Классы оружия, пропускаемые ночным режимом (23:00–06:00). Отбой проходит
# всегда (правило: відбій публікується завжди і миттєво).
NIGHT_CRITICAL_CLASSES = {"ballistic", "cruise_missile", "kab", "air_missile", "coastal_missile"}

# Маппинг канонических классов оружия в группы персонального фильтра
# («🎯 Типи тривог»): ballistic / uav / other. Классы вне словаря → 'other'.
WEAPON_PREF_GROUPS = {
    "ballistic": "ballistic", "cruise_missile": "ballistic", "kab": "ballistic",
    "air_missile": "ballistic", "coastal_missile": "ballistic", "missile": "ballistic",
    "shahed": "uav", "uav": "uav", "fpv": "uav", "recon_drone": "uav",
    "mlrs": "other", "artillery": "other", "explosion": "other",
    "air_defense": "other",
}


def weapon_group(weapon_class: str) -> str:
    """Группа класса для фильтра типов. '' = отбой/неизвестно — фильтр не применяется."""
    if not weapon_class or weapon_class == "stand_down":
        return ""
    return WEAPON_PREF_GROUPS.get(weapon_class, "other")


# Callback-префиксы фидбека под алертами в ЛС.
CB_FEEDBACK_USEFUL = "fbu:"
CB_FEEDBACK_NOISE = "fbn:"

# Ссылка для кнопки «Поділитися» (переопределяется из settings в run()).
PROMO_URL = "https://t.me/AirRadarAI"


def _notify_regions(fact, regions: list[str]) -> list[str]:
    """Регионы рассылки алерта: цель фьюжена, а не все упомянутые области.

    detect_region возвращает ВСЕ области, упомянутые в посте (происхождение
    + цель). В сборных сводках мониторинга это пол-Украины, и подписчик
    «Київ — лівий берег» получал каждый такой пост. Правило: доставляем по
    фактической цели (fact.destination_region); полные списки упоминаний —
    только когда цель неизвестна ('' / 'unknown') или пост честно помечен
    как 'multi' (затронуты действительно несколько областей).
    """
    destination = getattr(fact, "destination_region", "")
    if destination and destination not in ("unknown", "multi"):
        return [destination]
    return list(regions or [])


def _kyiv_hour() -> int:
    """Текущий час в Киеве (23:00–06:00 — ночное окно продукта).

    Украине без tzdata на Windows: переходы часов считаются вручную —
    последнее воскресенье марта/октября, момент 01:00 UTC в обоих случаях.
    """
    from datetime import datetime, timedelta, timezone as tz
    now = datetime.now(tz.utc)

    def _dst_transition(month: int) -> datetime:
        day = datetime(now.year, month, 31, 1, 0, tzinfo=tz.utc)  # 31 есть у обоих месяцев
        while day.weekday() != 6:  # воскресенье
            day -= timedelta(days=1)
        return day

    eest = _dst_transition(3) <= now < _dst_transition(10)
    return (now.hour + (3 if eest else 2)) % 24


def _is_night_time() -> bool:
    """Ночное окно 23:00–06:00 по киевскому времени."""
    hour = _kyiv_hour()
    return hour >= 23 or hour < 6


def _share_row(text: str) -> list:
    """Кнопка «Поділитися»: системное окно пересылки Telegram с готовым текстом.

    URL-кнопка t.me/share/url — работает у всех клиентов без inline-режима
    у бота: открывает выбор чата с предзаполненным заголовком алерта.
    """
    from urllib.parse import quote

    from telethon import Button
    head = (text or "").split("\n", 1)[0][:200]
    url = f"https://t.me/share/url?url={quote(PROMO_URL)}&text={quote(head)}"
    return [Button.url("↗ Поділитися", url)]


def _feedback_kb(message_key: str, text: str = "") -> list:
    """Кнопки «✅ корисно / ➖ шум» + «↗ Поділитися» под алертом в ЛС."""
    from telethon import Button
    rows = [[
        Button.inline("✅ Корисно", data=CB_FEEDBACK_USEFUL + message_key),
        Button.inline("➖ Шум", data=CB_FEEDBACK_NOISE + message_key),
    ]]
    if text:
        rows.append(_share_row(text))
    return rows


async def _notify_subscribers(bot_client, db: Database, regions: list[str], text: str,
                              feedback_key: str = "",
                              weapon_class: str = "") -> None:
    """Разослать текст всем подписчикам указанных регионов.

    Работает «best effort»: ошибки отправки (пользователь заблокировал бота и
    т.п.) логируются, но не роняют рассылку остальным. Текст отправляется как
    plain (без parse_mode), т.к. markdown в постах каналов часто ломается на
    спецсимволах, что молча блокировало всю рассылку.

    Київ-зоны: для slug='kyivska' определяем берег (detect_kyiv_zone).
    Если берег определён — рассылаем его подписчикам + подписчикам «всего
    Киева», иначе — подписчикам обоих берегов. Подписчики kyivska (город
    целиком) получают все киевские алерты независимо от берега.

    Ночной режим (персональный): 23:00–06:00 подписчик без галочки получает
    только критичные классы (ракеты/балістика/КАБ) — остальное копится в
    утренний дайджест. Отбой проходит всем всегда.

    Персональный фильтр типов (weapon_classes): подписчик с фильтром получает
    только разрешённые группы (ballistic/uav/other). Отбой и алерты без
    канонического класса фильтр не глушит — управляющие сообщения важнее.

    feedback_key: ключ поста для кнопок «✅ корисно / ➖ шум» (если задан).
    """
    # Ночной режим не глушит отбой: определяем по первой строке рендера.
    is_stand_down = text.startswith("🟢 ВІДБІЙ")
    night_now = _is_night_time()
    # Критичный класс ночью доставляется всем; некритичный ночью — только
    # подписчикам БЕЗ ночного режима.
    critical = is_stand_down or weapon_class in NIGHT_CRITICAL_CLASSES
    # Группа для персонального фильтра ('' = отбой/неизвестно — без фильтра).
    group = weapon_group(weapon_class)

    # Собираем уникальных подписчиков по всем регионам сообщения.
    # Пустой список = нечего рассылать (фьюжен не нашёл цель, а в тексте
    # не распознан ни один регион).
    notified: set[int] = set()
    sent_count = 0
    for slug in regions:
        targets = [slug]
        if slug == "kyivska":
            zone = detect_kyiv_zone(text)
            targets = (
                ["kyivska", zone] if zone
                else ["kyivska", "kyiv_left", "kyiv_right"]
            )
        for target in targets:
            for user_id in db.get_subscribers(target):
                if user_id in notified:
                    continue
                notified.add(user_id)
                if night_now and not critical and db.get_night_mode(user_id):
                    # Ночь, пост некритичный, у пользователя включён ночной
                    # режим — пропускаем (дайджест утром).
                    continue
                if group:
                    allowed = db.get_weapon_classes(user_id)
                    if allowed and group not in allowed.split(","):
                        # Фильтр типов включён, этой группы в нём нет.
                        continue
                try:
                    if feedback_key:
                        await bot_client.send_message(
                            user_id, text, link_preview=False,
                            buttons=_feedback_kb(feedback_key, text),
                        )
                    else:
                        await bot_client.send_message(user_id, text, link_preview=False)
                    sent_count += 1
                except Exception as exc:  # noqa: BLE001 — один неудачный не стопит остальных
                    logger.debug("Не удалось отправить подписку %s: %s", user_id, exc)
    if sent_count:
        logger.info("Рассылка по подпискам: отправлено %d получателям (регионы: %s)",
                    sent_count, ", ".join(region_name(s) for s in regions))


async def _healthcheck_loop(summarizer: SummarizerProtocol, interval: int) -> None:
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


# Интервал фоновой проверки «можлива нова тривога» (статистика волн).
PRE_WAVE_CHECK_INTERVAL = 60


async def _check_pre_wave(bot_client, db: Database) -> int:
    """Один проход: уведомить подписчиков о возможной новой волне.

    Для каждого региона с недавним отбоем проверяем статистику волн
    (pre_wave_notice): окно «медиана паузы ± LEAD/LAG» + не отправляли
    ли уже на этот эпизод. Отправка идёт через _notify_subscribers —
    ночной режим фильтрует получателей так же, как обычные алерты.
    Возвращает число отправленных уведомлений (для тестов и лога).
    """
    from wave_forecast import format_pre_wave_notice, pre_wave_notice

    sent = 0
    for row in db.recently_ended_alerts(8 * 3600):  # окно = NEXT_WAVE_MAX_GAP_S
        slug = row["region"]
        info = pre_wave_notice(db, slug)
        if not info.get("notify"):
            continue
        text = format_pre_wave_notice(slug, info)
        await _notify_subscribers(bot_client, db, [slug], text, weapon_class="")
        db.record_wave_notice(slug, int(info["episode_end_ts"]))
        sent += 1
        logger.info(
            "Предупреждение «можлива тривога»: %s (медіана ~%d хв)",
            slug, info.get("median_minutes", 0),
        )
    return sent


async def _pre_wave_loop(bot_client, db: Database, interval: int = PRE_WAVE_CHECK_INTERVAL) -> None:
    """Фоновый цикл проактивных предупреждений о возможной новой волне."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await _check_pre_wave(bot_client, db)
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                logger.exception("Сбой проверки «можлива нова тривога»")
    except asyncio.CancelledError:
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


# Глобальная ссылка на Publisher — для /summary (ручной запуск сводки).
_publisher_ref = None


def _get_publisher():
    """Возвращает Publisher (устанавливается в run()). Для admin_ui /summary."""
    return _publisher_ref


if __name__ == "__main__":
    main()
