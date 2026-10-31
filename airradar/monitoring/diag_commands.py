"""diag_commands.py — адмін-діагностика в боті: /diag [1h|24h|7d|30d], /sources, /queue, /backup_status, /trace, /replay.

Кожна команда: перевіряє права, пише в admin_audit, не показує секрети, нічого небезпечного не виконує
(/replay — лише сухий прогін, без відправок).
"""
from __future__ import annotations

import html

from telethon import events

from airradar.monitoring import dashboard, incident_trace
from airradar.ops import backup
from airradar.security import admin_access


def register_diag_handlers(bot, db, super_admin_id: int) -> None:
    def _is_admin(uid: int) -> bool:
        return db.is_admin(uid, super_admin_id)

    async def _guard(event, action: str, detail: str = "") -> bool:
        if not _is_admin(event.sender_id):
            return False  # мовчимо: не підтверджуємо існування команди сторонньому
        admin_access.log_action(db, event.sender_id, action, None, True, detail)
        return True

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/diag(?:\s+(1h|24h|7d|30d))?\s*$"))
    async def _diag(event) -> None:  # noqa: ANN001
        period = event.pattern_match.group(1) or "24h"
        if await _guard(event, "diag", period):
            await event.respond(dashboard.render_text(dashboard.collect(db, period)), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/sources\s*$"))
    async def _sources(event) -> None:  # noqa: ANN001
        if not await _guard(event, "sources"):
            return
        srcs = dashboard.collect(db, "24h")["sources"]
        if srcs is None:
            await event.respond("Джерела: unknown")
            return
        lines = ["📡 <b>Джерела</b>"]
        for s in srcs[:40]:
            age = "unknown" if s["last_seen_age_s"] is None else f"{s['last_seen_age_s'] // 60} хв тому"
            flag = "🚫" if s["disabled"] else ("⚠️" if s["errors"] else "✅")
            lines.append(f"{flag} {html.escape(s['channel'])}: {age}" + (f", помилок {s['errors']}" if s["errors"] else ""))
        await event.respond("\n".join(lines), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/queue\s*$"))
    async def _queue(event) -> None:  # noqa: ANN001
        if not await _guard(event, "queue"):
            return
        d = dashboard.collect(db, "1h")["delivery"]
        await event.respond("📬 <b>Черга сповіщень (1 год)</b>\n" + "\n".join(f"{k}: {v if v is not None else 'unknown'}" for k, v in d.items()), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/backup_status\s*$"))
    async def _backup_status(event) -> None:  # noqa: ANN001
        if not await _guard(event, "backup_status"):
            return
        age = backup.last_backup_age_s()
        await event.respond("💾 Остання резервна копія: " + ("unknown" if age is None else f"{age // 3600} год {age % 3600 // 60} хв тому"))

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/trace\s+(\S{1,80})\s*$"))
    async def _trace(event) -> None:  # noqa: ANN001
        key = event.pattern_match.group(1)
        if not await _guard(event, "trace", key[:60]):
            return
        tl = incident_trace.timeline(db, key)
        if not tl:
            await event.respond("Подію не знайдено.")
            return
        t0 = tl[0]["ts"]
        lines = [f"🔎 <b>{html.escape(key)}</b>"]
        for s in tl:
            lines.append(f"+{int((s['ts'] - t0) * 1000)}мс {'✅' if s['ok'] else '⛔'} {s['stage']} {html.escape(s['source'] or '')}")
        await event.respond("\n".join(lines[:40]), parse_mode="html")

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/replay\s+(\S{1,80})\s*$"))
    async def _replay(event) -> None:  # noqa: ANN001
        key = event.pattern_match.group(1)
        if not await _guard(event, "replay", key[:60]):
            return
        r = incident_trace.replay(db, key)
        await event.respond("🔁 Replay (сухий прогін, нічого не надіслано): " + html.escape(str({k: r[k] for k in r if k in ("replayed", "policy_allowed", "policy_violations", "original_outcome", "reason")})), parse_mode="html")
