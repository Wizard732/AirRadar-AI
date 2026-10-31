"""user_commands.py — короткі команди: /my /status /history /shelters /family /settings /help /checklist /insights."""
from __future__ import annotations

from telethon import events

import menu_screens as ms
import personal_status
import user_extras

HELP = (
    "🆘 <b>Команди AirRadar AI</b>\n\n"
    "/my — мій персональний статус\n/status — поточна ситуація\n/history — історія атак\n"
    "/shelters — найближче укриття\n/family — родина\n/settings — гео-сторож і налаштування\n"
    "/checklist — чек-лист безпеки\n/insights — моя статистика\n/calendar — календар тривог\n/voice — голосова зведення\n/offline — офлайн-пам'ятка\n/map — карта\n\n"
    + personal_status.DISCLAIMER
)


def register_user_commands(bot, db, map_webapp_url: str = "", is_admin=lambda uid: False) -> None:
    def cmd(name: str):
        return events.NewMessage(incoming=True, pattern=rf"^/{name}(?:@\w+)?\s*$", func=lambda e: getattr(e, "is_private", True))

    @bot.on(cmd("my"))
    async def _my(event) -> None:  # noqa: ANN001
        await event.respond(personal_status.render(db, event.sender_id), parse_mode="html")

    @bot.on(cmd("status"))
    async def _status(event) -> None:  # noqa: ANN001
        if is_admin(event.sender_id):
            return  # адмінам відповідає admin_ui.status (стан каналів)
        await event.respond(personal_status.render(db, event.sender_id), parse_mode="html")

    @bot.on(cmd("history"))
    async def _history(event) -> None:  # noqa: ANN001
        text, rows = ms.history_screen(db, map_webapp_url)
        await event.respond(text, parse_mode="html", buttons=rows)

    @bot.on(cmd("shelters"))
    async def _shelters(event) -> None:  # noqa: ANN001
        text, rows = ms.shelter_pick_screen(db, event.sender_id)
        await event.respond(text, parse_mode="html", buttons=rows)

    @bot.on(cmd("family"))
    async def _family(event) -> None:  # noqa: ANN001
        text, rows = ms.family_screen(db, event.sender_id)
        await event.respond(text, parse_mode="html", buttons=rows)

    @bot.on(cmd("settings"))
    async def _settings(event) -> None:  # noqa: ANN001
        text, rows = ms.guard_screen(db, event.sender_id, map_webapp_url)
        await event.respond(text, parse_mode="html", buttons=rows)

    @bot.on(cmd("help"))
    async def _help(event) -> None:  # noqa: ANN001
        await event.respond(HELP, parse_mode="html")

    @bot.on(cmd("checklist"))
    async def _checklist(event) -> None:  # noqa: ANN001
        from telethon import Button
        done = user_extras.done(db, event.sender_id)
        rows = [[Button.inline(("✅ " if k in done else "⬜️ ") + label[2:].strip()[:40], data="chk:" + k)] for k, label in user_extras.CHECKLIST]
        await event.respond(user_extras.checklist_text(db, event.sender_id), parse_mode="html", buttons=rows)

    @bot.on(cmd("insights"))
    async def _insights(event) -> None:  # noqa: ANN001
        await event.respond(user_extras.insights_text(db, event.sender_id), parse_mode="html")

    @bot.on(cmd("calendar"))
    async def _calendar(event) -> None:  # noqa: ANN001
        await event.respond(user_extras.calendar_text(db, event.sender_id), parse_mode="html")

    @bot.on(cmd("voice"))
    async def _voice(event) -> None:  # noqa: ANN001
        import io

        from airradar.product import voice
        text = voice.voice_text(db, event.sender_id)
        audio = await voice.synthesize(text)
        if audio:
            f = io.BytesIO(audio)
            f.name = "airradar_voice.mp3"
            await bot.send_file(event.sender_id, f, caption=text, voice_note=False)
        else:
            await event.respond("🔊 " + text)

    @bot.on(cmd("offline"))
    async def _offline(event) -> None:  # noqa: ANN001
        await event.respond(user_extras.OFFLINE_TEXT, parse_mode="html")
