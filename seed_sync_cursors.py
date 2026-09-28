#!/usr/bin/env python3
"""Одноразовый бутстрап курсоров recovery-sync перед первым запуском таймера.

sync_history.py при пустых курсорах читает канал С САМЫХ СТАРЫХ сообщений
(min_id=0, reverse=True) — на живом канале это годы истории и дубли в threats.
Скрипт ставит курсор каждого источника на id последнего сообщения канала,
чтобы таймер догонял только новое. Ничего не импортирует.

Запуск на CT100: cp сессии БОТА запрещён держать открытой — используется
временная КОПИЯ /opt/airradar/airradar.session (паттерн recovery-sync).
"""
from __future__ import annotations

import asyncio
import logging
import shutil

from telethon import TelegramClient

from config import load_settings
from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("seed_sync_cursors")


async def run() -> None:
    settings = load_settings()
    db = Database(settings.db_path)
    session_copy = f"/tmp/{settings.session_name}_history_seed"
    shutil.copyfile(f"{settings.db_path.rsplit('/', 1)[0]}/{settings.session_name}.session"
                    if "/" in settings.db_path else f"{settings.session_name}.session",
                    session_copy + ".session")
    client = TelegramClient(session_copy, settings.tg_api_id, settings.tg_api_hash)
    await client.start()
    try:
        for channel in settings.source_channels:
            try:
                entity = await client.get_entity(channel)
                source = str(getattr(entity, "username", None) or getattr(entity, "id", channel))
                last_id, _ = db.get_history_cursor(source)
                if last_id > 0:
                    log.info("%s: курсор уже стоит (%s), пропускаю", source, last_id)
                    continue
                msgs = await client.get_messages(entity, limit=1)
                if not msgs:
                    log.warning("%s: пустой канал, пропускаю", channel)
                    continue
                top = msgs[0]
                db.update_history_cursor(source, top.id, int(top.date.timestamp()))
                log.info("%s: курсор = message_id %s (%s)", source, top.id, top.date)
            except Exception as exc:  # noqa: BLE001 — один битый канал не стопит остальные
                log.error("Канал %s: %s", channel, exc)
    finally:
        await client.disconnect()
        db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
