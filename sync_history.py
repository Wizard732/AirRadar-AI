#!/usr/bin/env python3
"""Догрузка Telegram-истории в AirRadar с курсорами и защитой от дублей.

Первый запуск читает доступную историю SOURCE_CHANNELS. Последующие запуски
всё равно перечитывают недавнее окно, поэтому восстанавливают пропуски после
простоя, но не создают повторов по ключу channel/message_id.

Примеры:
    py -3 sync_history.py
    py -3 sync_history.py --since 2024-01-01 --limit 10000
    py -3 sync_history.py --channel @my_source --reconcile-days 7
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from analytics import _detect_stage
from config import load_settings
from database import Database
from fast_filter import clean_signature, matches_keywords
from regions import detect_region
from sticker import classify_threat
from weapon_classes import classify_weapon

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("history_sync")


def event_fields(text: str) -> tuple[str, str, str]:
    """Вернуть legacy-тип, класс оружия и нормализованную стадию события."""
    lowered = text.lower()
    # В классификаторе «тривога» идёт раньше «відбій», поэтому приоритет
    # явного отбоя нужен для корректного закрытия исторического интервала.
    weapon = "stand_down" if any(word in lowered for word in ("відбій", "отбой")) else classify_weapon(text)
    stage = _detect_stage(text, weapon)
    if weapon == "stand_down":
        return classify_threat(text), weapon, "all_clear"
    if stage == "past":
        return classify_threat(text), weapon, "impact"
    if weapon == "air_defense":
        return classify_threat(text), weapon, "intercept"
    if weapon == "alert":
        return classify_threat(text), weapon, "alert"
    return classify_threat(text), weapon, "launch" if "пуск" in text.lower() else "movement"


async def sync_channel(client: TelegramClient, db: Database, channel: str, since: datetime | None,
                       reconcile_days: int, limit: int | None) -> dict[str, int]:
    entity = await client.get_entity(channel)
    source = str(getattr(entity, "username", None) or getattr(entity, "id", channel))
    last_id, _ = db.get_history_cursor(source)
    # A zero-day recovery run uses only the cursor. Daily reconciliation may
    # intentionally replay a wider recent window to repair missed deliveries.
    if reconcile_days:
        reconcile_from = datetime.now(timezone.utc) - timedelta(days=reconcile_days)
        reconcile_min_id = db.get_history_reconcile_min_id(source, int(reconcile_from.timestamp()))
        min_id = max(0, reconcile_min_id - 1) if reconcile_min_id else last_id
    else:
        min_id = last_id
    if since is not None:
        min_id = 0

    stats = {"read": 0, "saved": 0, "skipped": 0, "unknown": 0}
    async for message in client.iter_messages(entity, min_id=min_id, reverse=True, limit=limit):
        if not message.id or not message.date:
            continue
        event_ts = int(message.date.timestamp())
        if since is not None and message.date < since:
            continue
        stats["read"] += 1
        if not db.claim_history_message(source, message.id, event_ts):
            stats["skipped"] += 1
            db.update_history_cursor(source, message.id, event_ts)
            continue
        text = clean_signature((message.message or "").strip())
        if not text or not matches_keywords(text):
            stats["skipped"] += 1
            db.update_history_cursor(source, message.id, event_ts)
            continue
        threat_type, weapon, stage = event_fields(text)
        regions = detect_region(text, channel=source) or ["unknown"]
        if regions == ["unknown"]:
            stats["unknown"] += 1
        outcome = "impact" if stage == "impact" else "unknown"
        if stage == "intercept":
            outcome = "intercept"
        for region in regions:
            is_new = db.add_event(event_ts=event_ts, weapon_class=weapon, stage=stage, region=region,
                                  text=text, source=source, outcome=outcome, confidence=0.75)
            if is_new:
                db.add_threat(threat_type, region, text, source, event_ts=event_ts)
                stats["saved"] += 1
            if region != "unknown" and stage == "alert":
                db.alert_start(region, event_ts=event_ts)
            elif region != "unknown" and stage == "all_clear":
                db.alert_end(region, event_ts=event_ts)
        db.update_history_cursor(source, message.id, event_ts)
    log.info("%s: read=%d saved=%d skipped=%d unknown=%d", source, stats["read"], stats["saved"], stats["skipped"], stats["unknown"])
    return stats


def parse_since(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Дата должна быть в формате YYYY-MM-DD") from exc


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    db = Database(settings.db_path)
    channels = [args.channel] if args.channel else settings.source_channels
    total = {"read": 0, "saved": 0, "skipped": 0, "unknown": 0}
    # The recovery service receives a temporary copy of the authenticated live
    # session. It must not open the live SQLite session while the bot uses it.
    client = TelegramClient(f"/tmp/{settings.session_name}_history", settings.tg_api_id, settings.tg_api_hash)
    await client.start()
    try:
        for channel in channels:
            try:
                stats = await sync_channel(client, db, channel, args.since, args.reconcile_days, args.limit)
                for key, value in stats.items():
                    total[key] += value
            except FloodWaitError as exc:
                log.error("Telegram запросил паузу %s сек. Повторите синхронизацию после паузы.", exc.seconds)
            except (RPCError, ValueError) as exc:
                # Numeric/private IDs may not be resolvable by this recovery
                # session. Skip one bad source instead of failing all channels.
                log.error("Канал %s недоступен: %s", channel, exc)
        log.info("ИТОГО: read=%d saved=%d skipped=%d unknown=%d", total["read"], total["saved"], total["skipped"], total["unknown"])
    finally:
        await client.disconnect()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Синхронизация исторических сообщений Telegram.")
    parser.add_argument("--since", type=parse_since, help="Первая дата для начальной загрузки (YYYY-MM-DD).")
    parser.add_argument("--channel", help="Один канал из SOURCE_CHANNELS для контролируемого запуска.")
    parser.add_argument("--limit", type=int, help="Лимит сообщений на канал.")
    parser.add_argument("--reconcile-days", type=int, default=7, help="Сколько последних дней перечитывать (по умолчанию: 7).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit должен быть положительным")
    if args.reconcile_days < 0:
        parser.error("--reconcile-days должен быть не меньше 0")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
