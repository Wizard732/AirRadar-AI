"""Отслеживание user-каналов модуля Interests (вынесено из main.run() без изменения логики).

Каналы добавляются пользователями через /add_channel и должны начать парситься
без рестарта: трекер хранит «голые» id уже найденных в диалогах чатов, а
``refresh_loop`` раз в минуту подтягивает новые.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("airradar")


def channel_key(raw: str) -> str:
    """Нормализовать '@Name' / 'name' / '-100123' / '123' к ключу сверки."""
    v = (raw or "").strip().lower().lstrip("@")
    if v.startswith("-100") and v[4:].isdigit():
        return v[4:]
    if v.startswith("-") and v[1:].isdigit():
        return v[1:]
    return v


def bare_chat_id(cid: int) -> int:
    """Маркированный id (-100…) → «голый» id канала для сверки."""
    if cid < 0:
        s = str(cid)
        return int(s[4:]) if s.startswith("-100") else int(s[1:])
    return cid


class InterestsChannelTracker:
    def __init__(self, client, db) -> None:
        self.client = client
        self.db = db
        self.ids: set[int] = set()            # «голые» id чатов user-каналов
        self.resolved_keys: set[str] = set()  # ключи каналов, уже найденные в диалогах

    def is_tracked(self, chat_id) -> bool:
        if chat_id is None:
            return False
        return bare_chat_id(chat_id) in self.ids

    async def resolve_new(self) -> None:
        """Найти в диалогах user-каналы, добавленные с прошлого обхода."""
        wanted = {channel_key(ch) for ch in self.db.all_interests_channels()}
        missing = wanted - self.resolved_keys
        if not missing:
            return
        found: dict[str, int] = {}
        async for dialog in self.client.iter_dialogs():
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
            self.resolved_keys.add(ch)
            if eid not in self.ids:
                self.ids.add(eid)
                logger.info("Interests: подключён user-канал id=%s", eid)
        if missing:
            logger.info(
                "Interests: жду подписки аккаунта на каналы: %s",
                ", ".join(sorted(missing)),
            )

    async def refresh_loop(self, interval: int = 60) -> None:
        """Раз в минуту подтягивать новые user-каналы без рестарта."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.resolve_new()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                logger.exception("Сбой обновления user-каналов Interests")
