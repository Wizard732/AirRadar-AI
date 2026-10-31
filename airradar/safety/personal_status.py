"""personal_status.py — «Мій AirRadar»: персональний статус, де офіційна тривога й аналітика розділені.

Три незалежні блоки: ОФІЦІЙНА тривога (сирени alerts.in.ua), ЗА ЗВЕДЕННЯМИ КАНАЛІВ (alarm_state) і АНАЛІТИКА AirRadar
(цілі поблизу збережених місць, без гарантованого часу підльоту). Єдиного «індексу небезпеки» навмисно немає.
"""
from __future__ import annotations

import html
import time

from regions import region_name

DISCLAIMER = ("ℹ️ AirRadar AI — інформаційно-аналітична система. Вона не замінює офіційні повідомлення про повітряну "
              "тривогу та рекомендації ДСНС. У разі тривоги негайно прямуйте до укриття.")


def user_regions(db, uid: int) -> list[str]:
    regs = list(db.user_subscriptions(uid))
    home = db.get_home_region(uid)
    if home and home not in regs:
        regs.insert(0, home)
    return regs


def official_state(db, regions: list[str], now: int) -> tuple[str, list[str]]:
    """('active'|'none'|'no_data', [регіони з тривогою])."""
    try:
        with db._lock:  # noqa: SLF001
            n = db._conn.execute("SELECT COUNT(*) FROM siren_states WHERE updated_ts>=?", (now - 900,)).fetchone()[0]  # noqa: SLF001
    except Exception:  # noqa: BLE001
        n = 0
    if not n:
        return "no_data", []
    active = set(db.siren_states_fresh(900))
    hit = [r for r in regions if r in active]
    return ("active" if hit else "none"), hit


def channel_state(db, regions: list[str], now: int) -> list[str]:
    from shelter_card import open_regions
    opened = open_regions(db, now)
    return [r for r in regions if r in opened]


def render(db, uid: int, now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    regs = user_regions(db, uid)
    if not regs:
        return ("🛡 <b>Мій AirRadar</b>\n\nЩоб бачити персональний статус, підпишись на область "
                "(🔔 Підписки) або задай «Мою зону».\n\n" + DISCLAIMER)
    names = ", ".join(region_name(r) for r in regs[:3]) + (" …" if len(regs) > 3 else "")
    off, hit = official_state(db, regs, now)
    lines = [f"🛡 <b>Мій AirRadar</b> — {html.escape(names)}", ""]
    if off == "active":
        lines.append("🚨 <b>Офіційна тривога: АКТИВНА</b> — " + ", ".join(region_name(r) for r in hit))
        lines.append("Перебувайте в укритті та дотримуйтесь вказівок місцевої влади.")
    elif off == "none":
        lines.append("🟢 Офіційна тривога: немає (за даними сирен)")
    else:
        lines.append("⚪️ Офіційна тривога: дані сирен недоступні — орієнтуйтесь на сирену й офіційні застосунки")
    ch = channel_state(db, regs, now)
    lines.append("📡 За зведеннями каналів: " + ("тривога у " + ", ".join(region_name(r) for r in ch) if ch else "тривоги не бачимо")
                 + " <i>(не офіційно)</i>")
    try:
        import shelter_card
        from track_engine import build_tracks, risk_for_point, risk_text
        targets = shelter_card.user_targets(db, uid, now)
        if targets:
            tracks = [t for t in build_tracks(db.recent_observations(now - 3 * 3600), now, window_s=3 * 3600) if t.status != "stale"]
            lines += ["", "🔎 <b>Аналітика AirRadar</b> (орієнтовно, не гарантія):"]
            for name, point, radius in targets:
                risks = risk_for_point(tracks, point, now, imminent_km=radius, watch_km=radius * 3)
                lines.append(f"📍 {html.escape(name)}: " + (risk_text(risks[0]) if risks else "поруч цілей зі зведень не бачу"))
    except Exception:  # noqa: BLE001 — аналітика не ламає офіційний статус
        pass
    from kyiv_time import fmt
    lines += ["", f"Оновлено: {fmt(now)}", "", DISCLAIMER]
    return "\n".join(lines)
