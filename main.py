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
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
from telethon import TelegramClient, events
from telethon.tl.custom import Message

import config
from aggregator import AlertAggregator, PendingAlert
from ai_summarizer import SummarizerProtocol, is_ignored_summary, make_summarizer
from alert_renderer import render_evidence_alert
from bot_ui import register_handlers
from city_coords import CITIES, detect_city
from database import Database
from dedup import DedupCache
from fast_filter import clean_signature, matches_keywords
from health_server import start_health_server
from airradar.ingestion.sirens_poll import SIRENS_POLL_INTERVAL, _sirens_fetch, _sirens_loop  # noqa: F401 — виділено з main.py
from publisher import Publisher
from regions import detect_kyiv_zone, detect_region, region_name
from incident_fusion import extract_incident_fact
from threat_parser import parse_post
from source_policy import normalize_source, source_group
from sticker import get_sticker_header
from track_speed import build_track

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
        settings.groq_model if settings.llm_backend == "groq" else (os.getenv("CVC_MODEL", "deepseek-v4.1-flash") if settings.llm_backend == "cvc" else settings.ollama_model),
    )

    # --- SQLite-журнал угроз/тревог (фундамент статистики и ETA) ---
    db = Database(settings.db_path)
    logger.info("БД журнала: %s", settings.db_path)
    # Версіоновані міграції (лише додаткові); перед застосуванням — онлайн-копія БД поруч із файлом.
    try:
        from pathlib import Path as _P
        from airradar.delivery import notify_queue as _nq
        from airradar.ops import migrations as _mig
        _applied = _mig.apply_pending(db, backup_before=_P(settings.db_path).with_suffix(".pre-migration.db"))
        if _applied:
            logger.info("Застосовано міграції БД: %s", _applied)
        _rec = _nq.recover_after_restart(db)
        if _rec:
            logger.info("Черга сповіщень: відновлено %d записів після перезапуску", _rec)
    except Exception:  # noqa: BLE001 — міграції не повинні вкласти бота; помилка видна в логах і /diag
        logger.exception("Міграції/відновлення черги не виконано")

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
    global _publisher_ref, _summarizer_ref
    _publisher_ref = publisher
    _summarizer_ref = summarizer

    # Дедупликация: гибрид in-memory + SQLite. Переживает рестарт и ловит
    # поздние репосты между каналами (окно в БД — до 1 часа).
    dedup = DedupCache(ttl=settings.dedup_ttl, db=db)

    # Агрегатор: сообщения одного инцидента в окне AGGREGATE_WINDOW_SEC
    # уходят в канал одним постом (критичные/отбой — мгновенно, байпасом).
    # Гард ложных CRITICAL («загроза балістики» от одного канала): одиночный
    # пост критичного класса не публикуется, пока инцидент не подтвердит
    # второй независимый источник (настройка CRITICAL_NEEDS_CONFIRMATION).
    aggregator = AlertAggregator(
        settings.aggregate_window_sec,
        lambda items: _publish_items(db, publisher, bot_client, items),
        critical_needs_confirmation=getattr(settings, "critical_needs_confirmation", False),
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
        # Гео-сторож ДО «Моя зона»/geo-репорта: локация включённого сторожа не должна
        # становиться публичным репортом на карте (StopPropagation внутри).
        from guard_handlers import register_guard_handlers
        register_guard_handlers(bot_client, db)
        register_handlers(bot_client, db, settings.admin_id, settings.webapp_url, settings.map_webapp_url)
        # Interests-модуль: команды /add_channel, /my_channels + кнопки тем.
        from interests_ui import register_interests_handlers
        register_interests_handlers(bot_client, db, settings.admin_id)
        # Админка (5.4): /status, /give_admin, /ban_channel.
        from admin_ui import register_admin_handlers
        register_admin_handlers(bot_client, db, settings.admin_id)
        from airradar.monitoring.diag_commands import register_diag_handlers
        register_diag_handlers(bot_client, db, settings.admin_id)
        from user_commands import register_user_commands
        register_user_commands(bot_client, db, settings.map_webapp_url, lambda uid: db.is_admin(uid, settings.admin_id))
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
    # Без интерактивного ввода: на сервере нет терминала, input() падал с EOFError и бот
    # молча перезапускался в цикле. Теперь — внятная ошибка, уведомление админу и
    # код выхода 2 (systemd: RestartPreventExitStatus=2 не крутит рестарты).
    await client.connect()
    # В интерактивном терминале даём войти (телефон + код); в сервисе (stdin не TTY) — выходим.
    if not await client.is_user_authorized() and not sys.stdin.isatty():
        await _report_session_failure(settings)
        logger.error("Сессия Telegram не авторизована: войди вручную (python main.py в терминале) и перезапусти сервис.")
        await client.disconnect()
        raise SystemExit(2)
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
    # Публічні канали з SOURCE_CHANNELS, на які акаунт ще не підписаний (наприклад, щойно додані), — підписуємось самі.
    if missed:
        from telethon.tl.functions.channels import JoinChannelRequest
        for ch in list(missed):
            if not ch.startswith("@"):
                continue
            try:
                ent = await client.get_entity(ch)
                await client(JoinChannelRequest(ent))
                resolved_chats.append(ent)
                resolved_titles.append(getattr(ent, "title", None) or ch)
                missed.remove(ch)
                logger.info("Підписався на канал %s", ch)
            except Exception as exc:  # noqa: BLE001 — недоступний канал не повинен валити старт
                logger.warning("Не вдалося підписатись на %s: %s", ch, exc)
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

    # --- проактивные прогнозы (статистика волн): countdown отбоя во время
    #     тревоги + «можлива нова тривога» после отбоя. Уходят и в ЛС
    #     подписчиков, и в целевой канал; работают даже без бота-меню. ---
    pre_wave_task = None
    if settings.pre_wave_notice or settings.standdown_notice:
        pre_wave_task = asyncio.create_task(
            _pre_wave_loop(
                bot_client, db,
                pre_wave=settings.pre_wave_notice,
                standdown=settings.standdown_notice,
                publisher=publisher,
            ),
            name="pre-wave-notice",
        )
        logger.info(
            "Проактивные прогнозы включены (відбій-countdown: %s, можлива тривога: %s).",
            settings.standdown_notice, settings.pre_wave_notice,
        )

    # --- гео-сторож: персональные предупреждения «цель приближается» ---
    proximity_task = None
    if bot_client is not None:
        from proximity import proximity_loop
        proximity_task = asyncio.create_task(
            proximity_loop(db, bot_client, map_url=getattr(settings, "map_webapp_url", "") or ""),
            name="guard-proximity",
        )
        logger.info("Гео-сторож включён (проверка цели ↔ точки пользователя каждые 20 сек).")

    # --- предупреждения районам «можливо до вас» по трекам с измеренным курсом ---
    district_task = None
    watchdog_task = None
    analyzer_task = None
    alert_task = None
    queue_task = None
    push_queue_task = None
    outbox_task = None
    reminder_task = None
    # Одна «жива» зведення хвилі в каналі (редагується, а не спамить окремими постами).
    shelter_task = None
    if bot_client is not None:
        from shelter_card import card_loop
        shelter_task = asyncio.create_task(card_loop(db, bot_client, getattr(settings, "map_webapp_url", "") or ""), name="shelter-card")
    from wave_digest import digest_loop
    digest_task = asyncio.create_task(digest_loop(db, publisher), name="wave-digest")
    if bot_client is not None:
        from district_warn import district_loop
        district_task = asyncio.create_task(district_loop(db, bot_client), name="district-warn")
        watchdog_task = asyncio.create_task(_ingest_watchdog(bot_client, settings.admin_id, db=db), name="ingest-watchdog")
        import user_extras
        reminder_task = asyncio.create_task(user_extras.reminder_loop(db, bot_client), name="checklist-reminder")
        from ai_analyzer import analyzer_loop, make_llm
        global _analyzer_ref
        _analyzer_ref = make_llm(summarizer, http_session)
        analyzer_task = asyncio.create_task(analyzer_loop(db, bot_client, _analyzer_ref, settings.admin_id), name="ai-analyzer")
        from airradar.monitoring.alert_loop import alert_loop
        alert_task = asyncio.create_task(alert_loop(db, bot_client, settings.admin_id), name="admin-alerts")
        from airradar.delivery.tg_delivery import retry_worker
        queue_task = asyncio.create_task(retry_worker(bot_client, db), name="notify-retry")
        import push_fcm as _pf  # noqa: F401 — реєструє відправника 'fcm'
        from airradar.delivery import push_queue as _pq, webpush_push as _wp  # noqa: F401 — реєструє 'webpush'
        push_queue_task = asyncio.create_task(_pq.retry_worker(db), name="push-retry")
        publisher.outbox_db = db
        outbox_task = asyncio.create_task(publisher.outbox_worker(), name="channel-outbox")

    # --- официальный учёт сирен alerts.in.ua (опционально, по токену) ---
    sirens_task = None
    if settings.alerts_in_ua_token:
        sirens_task = asyncio.create_task(
            _sirens_loop(db, settings.alerts_in_ua_token, http_session=http_session, publisher=publisher, bot_client=bot_client),
            name="sirens-poll",
        )
        logger.info("Опрос сирен alerts.in.ua включён (раз в %d сек).", SIRENS_POLL_INTERVAL)

    # --- еженедельный пост «🎯 Точність прогнозів» (день/час по Києву) ---
    accuracy_task = None
    if settings.accuracy_post_enabled:
        accuracy_task = asyncio.create_task(
            _accuracy_post_loop(
                db, publisher,
                day=settings.accuracy_post_weekday,
                hour=settings.accuracy_post_hour,
            ),
            name="accuracy-post",
        )
        logger.info(
            "Недельный пост точности включён (день %d, %02d:00 за Києвом).",
            settings.accuracy_post_weekday, settings.accuracy_post_hour,
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
        for _task in (district_task, watchdog_task, analyzer_task, alert_task, queue_task, push_queue_task, outbox_task, reminder_task, digest_task, shelter_task):
            if _task is not None:
                _task.cancel()
                try:
                    await _task
                except asyncio.CancelledError:
                    pass
        if proximity_task is not None:
            proximity_task.cancel()
            try:
                await proximity_task
            except asyncio.CancelledError:
                pass
        if sirens_task is not None:
            sirens_task.cancel()
            try:
                await sirens_task
            except asyncio.CancelledError:
                pass
        if accuracy_task is not None:
            accuracy_task.cancel()
            try:
                await accuracy_task
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


from publish_dedup import PublishDedup
_DEDUP = PublishDedup()


def _impact_sources(settings) -> frozenset[str]:
    """Ограничение источников наслідків: пусто = все моніторингові канали; IMPACT_SOURCES/OFFICIAL_SOURCES сужают список."""
    extra = {normalize_source(x) for x in os.getenv("IMPACT_SOURCES", "").replace(";", ",").split(",") if x.strip()}
    base = {x.lstrip("@").lower() for x in getattr(settings, "official_sources", ()) or ()}
    # Пустой набор = принимаем все каналы мониторинга (фильтр по тексту делает impact_log).
    return frozenset(extra and (base | {x.lstrip("@").lower() for x in extra}) or set())


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
    _LAST_INGEST[0] = time.time()
    import health_checks as _hc
    _src_early = normalize_source(str(getattr(event.chat, "username", None) or getattr(event.chat, "id", "?")))
    _hc.record(_src_early, "received")
    from airradar.monitoring import incident_trace as _trace
    _ekey = f"{_src_early}:{getattr(message, 'id', 0)}"
    _trace.record(db, _ekey, "received", source=_src_early, data={"text": text[:1000]})

    # 0) Очистка подписи канала ДО всех проверок — хвост «➡️Підписатись»
    #    засоряет фильтр, дедупликацию и определение региона.
    text = clean_signature(text)

    # 1) Быстрый фильтр по ключевым словам (до тяжёлого ИИ).
    if not matches_keywords(text):
        _hc.record(_src_early, "filtered")
        _trace.record(db, _ekey, "filter", source=_src_early, ok=False, data={"reason": "no_keywords"})
        return

    # 2) Дедупликация — повторы внутри TTL-окна игнорируем.
    if dedup.is_duplicate(text):
        logger.debug("Дубликат пропущен: %s", text[:60])
        _hc.record(_src_early, "duplicate")
        _trace.record(db, _ekey, "dedup", source=_src_early, ok=False, data={"reason": "duplicate"})
        return

    source = normalize_source(str(getattr(event.chat, "username", None) or getattr(event.chat, "id", "?")))
    if db.is_channel_disabled(source, "military"):
        logger.info("Отключённый источник пропущен: %s", source)
        _hc.record(source, "disabled")
        return
    logger.info("Новое сообщение от %s: %s", source, text[:80])
    # Отмечаем канал живым (для /status админки).
    db.channel_seen(source, "military")
    _hc.record(source, "processed")
    _trace.record(db, _ekey, "filter", source=source, data={"reason": "passed"})

    try:
        # 3) Classify before LLM: ordinary local news must never become a
        # fabricated weapon alert because a context-only filter matched it.
        from analytics import _detect_stage
        from weapon_classes import classify_weapon
        weapon = classify_weapon(text)
        event_ts = int(getattr(message, "date", None).timestamp()) if getattr(message, "date", None) else int(time.time())
        # Структурный разбор: новости/цитаты, события на чужой территории,
        # статистика сбитых и реклама — не живая угроза (раньше попадали на карту).
        parsed = parse_post(text, source)
        _trace.record(db, _ekey, "parse", source=source, data={"kind": parsed.kind, "observations": len(parsed.observations),
                                                               "oblasts": list(parsed.oblasts)[:8], "weapon": parsed.weapon})
        _trace.record(db, _ekey, "geo", source=source, ok=bool(parsed.observations or parsed.oblasts),
                      data={"places": [getattr(o, "place", "") for o in parsed.observations][:8]})
        # Наслідки (жертви, пошкодження) — только из официальных источников и с публикацией с задержкой.
        try:
            import impact_log
            rec = impact_log.record_from_post(db, text, source, event_ts, parsed, _impact_sources(settings))
            if rec and rec["kind"] == "hit" and os.getenv("PUBLISH_IMPACTS", "1") != "0" and time.time() - event_ts < 3600 \
                    and (rec["precision"] == "place" or rec["killed"] or rec["injured"] or rec["flags"] or rec["damage"]):
                await publisher.send(impact_log.impact_post_text(rec))
        except Exception:  # noqa: BLE001 — журнал наслідків не должен ронять приём постов
            logger.debug("impact_log не записал пост", exc_info=True)
        # Початок/кінець повітряної тривоги — окремі пости (стан по областях, без дублів і балаканини).
        try:
            import alarm_notices

            async def _notify(regs, msg):
                if bot_client is not None:
                    await _notify_subscribers(bot_client, db, regs, msg)
                import push_fcm
                await push_fcm.push_regions(db, regs, "🚨 ПОВІТРЯНА ТРИВОГА" if msg.startswith("🚨") else "🟢 Відбій", msg,
                                            "alarm" if msg.startswith("🚨") else "info")
            await alarm_notices.handle_post(db, publisher, _notify, text, source, event_ts)
        except Exception:  # noqa: BLE001 — сповіщення про тривогу не повинні валити конвеєр
            logger.exception("alarm_notices")
        if parsed.kind in {"news", "foreign", "stats", "promo"}:
            logger.info("Не угроза (%s) пропущено: %s", parsed.kind, text[:80])
            _trace.record(db, _ekey, "rejected", source=source, ok=False, data={"reason": parsed.kind})
            db.record_inference_audit(event_ts=event_ts, source=source, model="threat_parser", raw_output="", decision="rejected", reason=parsed.kind)
            return
        if weapon == "unknown" and parsed.kind == "track" and parsed.weapon:
            weapon = parsed.weapon  # «Чернігів, шахед на місто!» без ключевых слов классификатора
        stage = _detect_stage(text, weapon)
        if weapon == "unknown":
            logger.info("Неклассифицированный пост пропущен: %s", text[:80])
            _trace.record(db, _ekey, "rejected", source=source, ok=False, data={"reason": "unknown_weapon"})
            db.record_inference_audit(event_ts=event_ts, source=source, model=getattr(summarizer, "_model", "unknown"), raw_output="", decision="rejected", reason="unknown_weapon")
            return
        # 4) Сжатие через LLM только для уже распознанной угрозы.
        summary = await summarizer.summarize(text)
        if is_ignored_summary(summary):
            db.record_inference_audit(event_ts=event_ts, source=source, model=getattr(summarizer, "_model", "unknown"), raw_output=summary, decision="ignored")
            logger.info("Неподтверждённый звуковой пост пропущен: %s", text[:80])
            _trace.record(db, _ekey, "rejected", source=source, ok=False, data={"reason": "ignored_summary"})
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
        # Сводка по нескольким областям: берём ВСЕ области из парсера, а не
        # то, что нашла подстрока (раньше «Рух шахедів» по 8 областям = 1 регион).
        regions = list(parsed.oblasts) if parsed.kind == "track" and parsed.oblasts else detect_region(text, channel=source)
        if parsed.observations:
            db.add_observations(event_ts, str(source), parsed.observations, parsed.as_of)
        fact = extract_incident_fact(text, weapon, stage)
        group = source_group(source, getattr(settings, "source_groups", {}))
        # Официальный источник (OFFICIAL_SOURCES) подтверждает инцидент сразу:
        # «підтверджено офіційним джерелом» вместо обычной корроборации.
        is_official = str(source) in getattr(settings, "official_sources", frozenset())
        # Fusion owns the target location: in "from Sumy to Kyiv" Kyiv is the incident.
        confirmation = db.merge_incident_fact(
            event_ts=event_ts, source=source, source_group=group, fact=fact, text=text,
            window_seconds=getattr(settings, "incident_window_seconds", 1200),
            confirmation_sources=getattr(settings, "confirmation_sources", 2),
            official=is_official,
        )
        _trace.record(db, _ekey, "trust", source=source, data={"status": confirmation.get("status"), "merged": bool(confirmation.get("merged")),
                                                               "official": is_official, "incident": confirmation.get("key", "")[:40]})
        try:
            import event_lifecycle as _lc
            _k = confirmation.get("key", "")
            if _k:
                _lc.advance(db, _k, _lc.Stage.CREATED if not confirmation.get("merged") else _lc.Stage.OBSERVED)
                if confirmation.get("status") in ("corroborated", "officially_confirmed"):
                    _lc.advance(db, _k, _lc.Stage.VERIFIED)
                if confirmation.get("merged") and confirmation.get("material_update"):
                    _lc.advance(db, _k, _lc.Stage.UPDATED)
        except Exception:  # noqa: BLE001 — життєвий цикл не має ламати конвеєр
            logger.debug("event_lifecycle: збій", exc_info=True)
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
        import alarm_notices as _alarm
        publishable = (weapon == "stand_down" and not _alarm.suppress_old_standdown(text, source)) or (
            (stage == "imminent" or (stage == "unknown" and unknown_ok)) and public_class
        )
        # Непубликуемое (последствия удара, ППО, авиация) НЕ прерывает
        # конвейер ранним return: журнал БД ниже обязан зафиксировать
        # impact-событие, иначе ETA и статистика региона не получают
        # пары пуск→прилёт. Публикация при этом по-прежнему закрыта.
        _trace.record(db, _ekey, "safety", source=source, ok=publishable,
                      data={"publishable": publishable, "stage": stage, "weapon": weapon, "public_class": public_class})
        if not publishable:
            logger.info("Неподтверждённый/потенциальный пост не опубликован: %s", text[:80])
            _trace.record(db, _ekey, "rejected", source=source, ok=False, data={"reason": "not_publishable"})
        elif aggregator is not None:
            _trace.record(db, _ekey, "published", source=source, data={"via": "aggregator"})
            await aggregator.submit(
                PendingAlert(
                    text=text, source=source, event_ts=event_ts, fact=fact,
                    confirmation=confirmation, regions=regions,
                    received_ts=int(time.time()),
                )
            )
        else:
            # Fallback без агрегатора (прямые вызовы в тестах): прежнее поведение.
            try:
                track = build_track(
                    db, fact.destination_region, weapon, event_ts,
                    speed_profile=getattr(fact, "speed_profile", ""),
                )
            except Exception:  # noqa: BLE001 — трек не должен ронять публикацию
                logger.debug("build_track (fallback) не удался", exc_info=True)
                track = None
            final_text = render_evidence_alert(
                text=text, source=source, event_ts=event_ts, fact=fact,
                confirmation=confirmation, track=track
            )
            publication = db.incident_publication(confirmation.get("key", ""))
            _dup = False
            if publication and confirmation.get("material_update"):
                published = await publisher.edit(str(publication["published_chat_id"]), int(publication["published_message_id"]), final_text)
            elif not _DEDUP.allow(weapon, parsed, regions):
                logger.info("Повтор уже опубликованного — в канал не идёт: %s", text[:80])
                published = None
                _dup = True
            else:
                published = await publisher.send(final_text)
                if published and confirmation.get("key"):
                    chat = published.get("chat", {})
                    db.save_incident_publication(confirmation["key"], str(chat.get("id", "")), int(published.get("message_id", 0)))
            if not published and not _dup:
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
        # Направление «origin>destination» (slug'и) — сырьё для треков и
        # аналитики. Пишем только когда оба конца достоверно известны:
        # unknown/multi не дают вектора и только шумят выборку.
        direction = ""
        if (fact.origin_region and fact.destination_region
                and fact.origin_region not in ("unknown", "multi")
                and fact.destination_region not in ("unknown", "multi")):
            direction = f"{fact.origin_region}>{fact.destination_region}"
        for slug in regions_to_log:
            db.add_event(event_ts=event_ts, weapon_class=weapon, stage=event_stage, region=slug,
                         text=text, source=source, direction=direction,
                         outcome=outcome, confidence=0.8)
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


# Время последнего входящего поста с каналов: если лента молчит слишком долго, сессия
# Telegram, скорее всего, слетела или каналы отвалились, а бот при этом «жив».
_LAST_INGEST: list[float] = [time.time()]
INGEST_SILENCE_S = 90 * 60


async def _report_session_failure(settings) -> None:
    """Один раз в час шлёт админу через Bot API: сессия слетела. Файл-маркер переживает рестарты."""
    import json as _json
    marker = Path(settings.db_path).with_name(".session_alert")
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
            return
        import aiohttp
        from admin_notify import admin_recipients
        recipients = admin_recipients(Database(settings.db_path), settings.admin_id)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            for chat in recipients:
                await http.post(
                    f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
                    json={"chat_id": chat,
                          "text": "🔴 AirRadar: сессия Telegram не авторизована, бот остановлен. "
                                  "Нужен повторный вход (код из Telegram)."},
                )
        marker.write_text(_json.dumps({"ts": int(time.time())}))
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось отправить уведомление о слетевшей сессии", exc_info=True)


async def _ingest_watchdog(bot_client, admin_id: int, interval: int = 600, db: Database | None = None) -> None:
    last_alert = 0.0
    while True:
        await asyncio.sleep(interval)
        silent = time.time() - _LAST_INGEST[0]
        if silent > INGEST_SILENCE_S and time.time() - last_alert > 3 * 3600:
            last_alert = time.time()
            try:
                msg = (f"⚠️ Лента каналов молчит {int(silent // 60)} хв. Проверь сессию Telegram и источники "
                       "(journalctl -u airradar-bot).")
                if db is not None:
                    from admin_notify import notify_admins
                    await notify_admins(bot_client, db, admin_id, msg)
                else:
                    await bot_client.send_message(admin_id, msg)
            except Exception:  # noqa: BLE001
                logger.warning("watchdog: не удалось уведомить админа", exc_info=True)


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
    try:
        track = build_track(
            db, last.fact.destination_region, last.fact.weapon_class, last.event_ts,
            speed_profile=getattr(last.fact, "speed_profile", ""),
        )
    except Exception:  # noqa: BLE001 — трек не должен ронять публикацию
        logger.debug("build_track не удался", exc_info=True)
        track = None
    final_text = render_evidence_alert(
        text=last.text, source=last.source, sources=sources,
        event_ts=last.event_ts, fact=last.fact, confirmation=last.confirmation,
        track=track, lag_s=max(0, int(time.time()) - int(last.event_ts)),
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
    elif not _DEDUP.allow(last.fact.weapon_class, parse_post(last.text, getattr(last, "source", "")),
                          [slug for item in items for slug in item.regions if slug]):
        logger.info("Повтор уже опубликованного — в канал не идёт: %s", last.text[:80])
        return
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
    # Метрика задержки конвейера «пост источника → пост в канале»: от самого
    # старого received_ts набора до момента публикации (мс). Замер только для
    # реально опубликованных постов; legacy-алерты без received_ts пропускаем.
    received = [int(it.received_ts) for it in items if int(getattr(it, "received_ts", 0) or 0)]
    if received:
        try:
            db.record_pipeline_latency(int((time.time() - min(received)) * 1000))
            try:
                from airradar.monitoring import metrics as _metrics
                _metrics.record_stage(db, "total", int((time.time() - min(received)) * 1000))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — метрика не роняет публикацию
            logger.debug("pipeline latency: не удалось записать замер")
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
CB_FEEDBACK_ERROR = "fbe:"  # «❌ Помилка»: разметка ошибочных постов для обучения

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


def _kyiv_datetime():
    """Текущее время в Киеве как naive datetime (общий хелпер kyiv_time)."""
    from kyiv_time import kyiv_datetime
    return kyiv_datetime()


def _kyiv_hour() -> int:
    """Текущий час в Киеве (23:00–06:00 — ночное окно продукта)."""
    return _kyiv_datetime().hour


def _kyiv_date_str() -> str:
    """Текущая дата в Киеве «РРРР-ММ-ДД» (дедуп недельного поста по дате)."""
    return _kyiv_datetime().strftime("%Y-%m-%d")


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
    """Кнопки «✅ корисно / ➖ шум / ❌ помилка» + «↗ Поділитися» под алертом в ЛС.

    «Помилка» — разметка пользователями ошибочных постов: датасет для
    обучения точности (считается отдельным голосом в alert_feedback.error).
    """
    from telethon import Button
    rows = [[
        Button.inline("✅ Корисно", data=CB_FEEDBACK_USEFUL + message_key),
        Button.inline("➖ Шум", data=CB_FEEDBACK_NOISE + message_key),
    ], [
        Button.inline("❌ Помилка", data=CB_FEEDBACK_ERROR + message_key),
    ]]
    if text:
        rows.append(_share_row(text))
    return rows


async def _notify_subscribers(bot_client, db: Database, regions: list[str], text: str,
                              feedback_key: str = "",
                              weapon_class: str = "",
                              importance: str = "") -> None:
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

    feedback_key: ключ поста для кнопок «✅ корисно / ➖ шум / ❌ помилка» (если задан).
    """
    # Ночной режим не глушит отбой: определяем по первой строке рендера.
    is_stand_down = text.startswith(("🟢 ВІДБІЙ", "🚨 ПОВІТРЯНА ТРИВОГА"))
    night_now = _is_night_time()
    # Критичный класс ночью доставляется всем; некритичный ночью — только
    # подписчикам БЕЗ ночного режима.
    critical = is_stand_down or weapon_class in NIGHT_CRITICAL_CLASSES
    # Важливість для персонального фільтра типів: critical (тривога/відбій) не вимикається ніколи.
    from notification_rules import Importance as _Imp
    msg_importance = _Imp.CRITICAL if is_stand_down else _Imp(importance or "info")
    # Необов'язкова кнопка «Отримав» лише під офіційною тривогою (не під кожним сповіщенням).
    import hashlib as _hl
    ack_key = _hl.md5(text.encode()).hexdigest()[:16] if text.startswith("🚨 ПОВІТРЯНА ТРИВОГА") else ""
    # Группа для персонального фильтра ('' = отбой/неизвестно — без фильтра).
    group = weapon_group(weapon_class)
    # Область города из текста алерта: маркер «ваша зона» срабатывает и когда
    # пост не дошёл до подписчика его области через список регионов
    # (например, текст честно называет город, а фьюжен дал другую цель).
    city_region = ""
    city_slug = detect_city(text)
    if city_slug:
        city_region = str(CITIES.get(city_slug, ("", ""))[1] or "")

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
                if msg_importance is not _Imp.CRITICAL and msg_importance.value in db.get_notify_levels(user_id):
                    continue  # користувач вимкнув цей тип сповіщень
                if night_now and not critical and db.get_night_mode(user_id):
                    # Ночь, пост некритичный, у пользователя включён ночной
                    # режим — пропускаем (дайджест утром).
                    continue
                if group:
                    allowed = db.get_weapon_classes(user_id)
                    if allowed and group not in allowed.split(","):
                        # Фильтр типов включён, этой группы в нём нет.
                        continue
                home = db.get_home_region(user_id)
                zone_hit = bool(home) and (
                    home in regions
                    or (city_region != "" and city_region == home)
                )
                if db.get_shelter_mode(user_id) and not (critical or zone_hit):
                    # Режим «Укриття»: проходят только критичные (тревога),
                    # відбій и сообщения, касающиеся своей зоны.
                    continue
                out_text = f"🎯 Торкнеться вашої зони\n{text}" if zone_hit else text
                dkey = feedback_key or ack_key
                from airradar.delivery import tg_delivery
                # Доставка через персистентну чергу: ретраї з backoff, FloodWait, ідемпотентність, відновлення після рестарту.
                ok_sent = await tg_delivery.deliver(
                    bot_client, db, dkey or "bcast", user_id, out_text,
                    feedback_key=feedback_key, ack_key=ack_key, src_text=text,
                    ttl_s=3600 if critical else 900)
                if ok_sent:
                    sent_count += 1
                    if dkey:  # sent ≠ delivered: пишемо лише факт відправки
                        db.record_delivery(dkey, f"tg:{user_id}", "critical" if critical else "info", "sent")
                elif dkey:
                    db.record_delivery(dkey, f"tg:{user_id}", "critical" if critical else "info", "failed")
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


# Интервал фоновой проверки прогнозов (статистика волн):
# «відбій орієнтовно за ~N хв» во время тревоги и «можлива нова тривога»
# после отбоя.
PRE_WAVE_CHECK_INTERVAL = 60

# Интервал опроса официальных сирен alerts.in.ua (сек).


async def _check_standdown(bot_client, db: Database, publisher=None) -> int:
    """Один проход: countdown отбоя «відбій орієнтовно за ~N хв».

    Для каждого региона с активной тревогой проверяем standdown_notice:
    цель в воздухе + медиана «останній політ → відбій» по региону известна
    + остаток опустился до порога + на этот эпизод ещё не писали. Текст
    содержит «почему»: возраст последнего полётного поста и медиану
    статистики. Отправка через _notify_subscribers — ночной режим и
    фильтры работают как у обычных алертов (weapon_class='' — без
    фильтрации типов: управляющее сообщение). publisher: тот же текст
    дополнительно уходит в целевой канал (чат), а не только в ЛС.
    Возвращает число отправленных уведомлений (для тестов и лога).
    """
    from wave_forecast import format_standdown_notice, standdown_notice

    sent = 0
    for slug in db.active_alert_regions():
        info = standdown_notice(db, slug)
        if not info.get("notify"):
            continue
        text = format_standdown_notice(slug, info)
        if bot_client is not None:
            await _notify_subscribers(bot_client, db, [slug], text, weapon_class="", importance="analytic")
            sent += 1
        await _publish_notice(publisher, text)
        db.record_standdown_notice(
            slug,
            int(info["flight_ts"]),
            alert_started_ts=int(info["alert_started_ts"]),
            remaining_min=int(info["remaining_min"]),
        )
        logger.info(
            "Countdown отбоя: %s — орієнтовно ~%s хв (медіана ~%d хв)",
            slug, info.get("remaining_min", 0), info.get("median_minutes", 0),
        )
    return sent


async def _check_pre_wave(bot_client, db: Database, publisher=None) -> int:
    """Один проход: уведомить подписчиков о возможной новой волне.

    Для каждого региона с недавним отбоем проверяем статистику волн
    (pre_wave_notice): окно «медиана паузы ± LEAD/LAG» + не отправляли
    ли уже на этот эпизод. Отправка идёт через _notify_subscribers —
    ночной режим фильтрует получателей так же, как обычные алерты.
    publisher: тот же текст дополнительно уходит в целевой канал (чат).
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
        if bot_client is not None:
            await _notify_subscribers(bot_client, db, [slug], text, weapon_class="", importance="analytic")
            sent += 1
        await _publish_notice(publisher, text)
        db.record_wave_notice(slug, int(info["episode_end_ts"]))
        logger.info(
            "Предупреждение «можлива тривога»: %s (медіана ~%d хв)",
            slug, info.get("median_minutes", 0),
        )
    return sent


async def _publish_notice(publisher, text: str) -> None:
    """Опубликовать прогнозное уведомление в канал (best effort).

    Прогнозы (countdown отбоя, «можлива нова тривога») раньше уходили
    только в ЛС подписчикам. По правилу продукта они должны быть видны
    и в канале. Бренд-строка в конце — как у обычных постов (Publisher
    делает её кликабельной ссылкой на наш канал).
    """
    if publisher is None:
        return
    from alert_renderer import SOURCE_BRAND_LINE

    try:
        sent = await publisher.send(f"{text}\n\n{SOURCE_BRAND_LINE}")
        if not sent:
            logger.warning("Прогноз не опубликован в канал: отправка не удалась")
    except Exception as exc:  # noqa: BLE001 — прогноз не должен ронять цикл
        logger.warning("Прогноз не опубликован в канал: %s", exc)


async def _pre_wave_loop(bot_client, db: Database, interval: int = PRE_WAVE_CHECK_INTERVAL,
                         pre_wave: bool = True, standdown: bool = True,
                         publisher=None) -> None:
    """Фоновый цикл прогнозов: countdown отбоя + «можлива нова тривога»."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                if standdown:
                    await _check_standdown(bot_client, db, publisher)
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                logger.exception("Сбой проверки «відбій орієнтовно»")
            try:
                if pre_wave:
                    await _check_pre_wave(bot_client, db, publisher)
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                logger.exception("Сбой проверки «можлива нова тривога»")
    except asyncio.CancelledError:
        raise




_LAST_ALERTS: list[dict] = []




async def _accuracy_post_loop(db: Database, publisher=None, *,
                              day: int = 1, hour: int = 9,
                              check_interval: int = 300) -> None:
    """Еженедельный пост «🎯 Точність прогнозів» в канал.

    Публикуется в заданный день недели и час по киевскому времени
    (day: 0=понеділок … 6=неділя). Дедуп по киевской дате в памяти процесса:
    повторный пост в тот же день невозможен, рестарт в час публикации в
    худшем случае даёт второй пост — риск принят (пост еженедельный).
    """
    from wave_forecast import accuracy_post_text

    last_sent_date = ""
    while True:
        await asyncio.sleep(check_interval)
        kdt = _kyiv_datetime()
        if kdt.weekday() != day or kdt.hour != hour:
            continue
        today = kdt.strftime("%Y-%m-%d")
        if last_sent_date == today:
            continue
        last_sent_date = today
        try:
            text = accuracy_post_text(db)
        except Exception:  # noqa: BLE001 — пост не роняет цикл
            logger.exception("Не удалось собрать пост точности прогнозов")
            continue
        await _publish_notice(publisher, text)
        logger.info("Опубликован недельный пост «Точність прогнозів»")


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
_summarizer_ref = None
_analyzer_ref = None


def _get_summarizer():
    """Модель для /analyze: API-посередник, якщо налаштований, інакше модель проєкту."""
    return _analyzer_ref or _summarizer_ref


def _get_publisher():
    """Возвращает Publisher (устанавливается в run()). Для admin_ui /summary."""
    return _publisher_ref


if __name__ == "__main__":
    main()
