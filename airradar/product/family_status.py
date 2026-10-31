"""family_status.py — статуси родини: на зв'язку / в укритті / в безпеці / потрібна допомога / невідомо.

Відсутність відповіді = «невідомо» (може бути без інтернету чи розряджений телефон), а НЕ «в небезпеці».
Координати не показуються; статус дійсний обмежений час, після нього знову «невідомо».
"""
from __future__ import annotations

import html
import time

import family

STATUSES = {
    "ok": "🟢 На зв'язку",
    "shelter": "🏚 В укритті",
    "safe": "✅ В безпеці",
    "help": "🆘 Потрібна допомога",
}
UNKNOWN = "❔ Статус невідомий"
TTL_S = 6 * 3600


def _ensure(db) -> None:
    family.ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS family_status (user_id INTEGER PRIMARY KEY, status TEXT NOT NULL, ts INTEGER NOT NULL)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def set_status(db, uid: int, status: str, now: int | None = None) -> bool:
    if status not in STATUSES:
        return False
    _ensure(db)
    ts = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT INTO family_status(user_id,status,ts) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, ts=excluded.ts", (uid, status, ts))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    if status == "shelter":
        family.set_safe(db, uid, ts)  # зворотна сумісність: вимикає повтори сторожа
    return True


def get_status(db, uid: int, now: int | None = None) -> tuple[str, int | None]:
    """(код або 'unknown', час останньої відповіді)."""
    _ensure(db)
    now = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT status, ts FROM family_status WHERE user_id=?", (uid,)).fetchone()  # noqa: SLF001
    if not r:
        return "unknown", None
    if now - r[1] > TTL_S:
        return "unknown", r[1]
    return r[0], r[1]


def _hhmm(ts: int) -> str:
    from kyiv_time import fmt  # noqa: PLC0415
    return fmt(ts)


def board_text(db, uid: int, now: int | None = None) -> str:
    """Таблиця «Моя родина» для користувача uid."""
    now = int(now if now is not None else time.time())
    ms = family.members(db, uid)
    if not ms:
        return "Поки нікого в родині."
    lines = []
    for m in ms:
        code, ts = get_status(db, m, now)
        label = STATUSES.get(code, UNKNOWN)
        tail = f" · остання відповідь {_hhmm(ts)}" if ts else ""
        lines.append(f"• {html.escape(family.name_of(db, m))}: {label}{tail}")
    lines.append("\nℹ️ «Невідомо» не означає небезпеку — людина може бути без зв'язку.")
    return "\n".join(lines)


def status_message(db, uid: int, code: str) -> str:
    return f"👪 {html.escape(family.name_of(db, uid))}: {STATUSES[code]}"


ALARM_PROMPT = ("🚨 У вашому регіоні повітряна тривога.\nУсі члени родини можуть підтвердити свій статус:")


PROMPT_COOLDOWN_S = 30 * 60


def _prompt_due(db, uid: int, now: int) -> bool:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS family_prompt_log (user_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)")  # noqa: SLF001
        r = db._conn.execute("SELECT ts FROM family_prompt_log WHERE user_id=?", (uid,)).fetchone()  # noqa: SLF001
        if r and now - r[0] < PROMPT_COOLDOWN_S:
            return False
        db._conn.execute("INSERT INTO family_prompt_log(user_id,ts) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET ts=excluded.ts", (uid, now))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True


async def send_alarm_prompt(db, bot, regions: list[str], now: int | None = None) -> int:
    """При ОФІЦІЙНІЙ тривозі запропонувати підписникам регіону з родиною підтвердити статус (не частіше разу на 30 хв)."""
    from telethon import Button
    now = int(now if now is not None else time.time())
    _ensure(db)
    seen: set[int] = set()
    sent = 0
    for slug in regions:
        for uid in db.get_subscribers(slug):
            if uid in seen or not family.members(db, uid) or not _prompt_due(db, uid, now):
                continue
            seen.add(uid)
            try:
                await bot.send_message(uid, ALARM_PROMPT, silent=False, buttons=[
                    [Button.inline("🏚 Я в укритті", data="fam:st:shelter"), Button.inline("✅ Я в безпеці", data="fam:st:safe")],
                    [Button.inline("🆘 Потрібна допомога", data="fam:st:help")]])
                sent += 1
            except Exception:  # noqa: BLE001
                pass
    return sent
