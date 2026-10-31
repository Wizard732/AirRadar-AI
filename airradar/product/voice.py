"""voice.py — AirRadar Voice: коротка голосова зведення (/voice).

Текст формується ДЕТЕРМІНОВАНО з офіційних даних сирен і фіксованого шаблону — жодної «інтерпретації» небезпеки
моделлю. Озвучка — безкоштовний edge-tts (вихідні запити, без ключів); якщо недоступний — користувач отримує текст.
"""
from __future__ import annotations

import logging
import time

from regions import region_name

logger = logging.getLogger(__name__)
VOICE = "uk-UA-PolinaNeural"


def voice_text(db, uid: int, now: int | None = None) -> str:
    from personal_status import official_state, user_regions
    now = int(now if now is not None else time.time())
    regs = user_regions(db, uid)
    if not regs:
        return "Щоб отримати персональну голосову зведення, підпишіться на область у боті."
    state, hit = official_state(db, regs, now)
    if state == "active":
        names = ", ".join(region_name(r) for r in hit[:3])
        return (f"Увага. У вашому регіоні триває офіційна повітряна тривога: {names}. "
                "Перебувайте в укритті та дотримуйтесь вказівок місцевої влади.")
    if state == "none":
        return ("За даними офіційних сирен у вашому регіоні тривоги немає. "
                "AirRadar не замінює офіційні повідомлення. Почувши сирену, негайно прямуйте до укриття.")
    return "Дані офіційних сирен зараз недоступні. Орієнтуйтеся на сирену та офіційні повідомлення."


async def synthesize(text: str) -> bytes | None:
    """mp3-озвучка; None, якщо сервіс недоступний."""
    try:
        import edge_tts
        chunks = bytearray()
        async for ch in edge_tts.Communicate(text, VOICE).stream():
            if ch["type"] == "audio":
                chunks.extend(ch["data"])
        return bytes(chunks) or None
    except Exception:  # noqa: BLE001
        logger.warning("voice: синтез недоступний", exc_info=True)
        return None
