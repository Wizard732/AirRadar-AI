"""family.py — «Родина»: взаємні зв'язки між користувачами та статус «Я в укритті».

Додавання лише за згодою: власник створює посилання-запрошення (діє 48 год), інша людина відкриває його в боті —
і вони стають родиною ВЗАЄМНО. Далі кожному надходять короткі повідомлення про іншого: «⚠️ небезпечно біля 🏠 Дім»
(без координат — лише назва місця, відстань і час) та «✅ в укритті». Вийти з родини може будь-хто, видаливши зв'язок.
"""
from __future__ import annotations

import secrets
import time

MAX_MEMBERS = 5
INVITE_TTL_S = 48 * 3600
SAFE_TTL_S = 3 * 3600


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        c = db._conn  # noqa: SLF001
        c.execute("CREATE TABLE IF NOT EXISTS family_links (a INTEGER NOT NULL, b INTEGER NOT NULL, since_ts INTEGER NOT NULL, PRIMARY KEY (a, b))")
        c.execute("CREATE TABLE IF NOT EXISTS family_invites (code TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, ts INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS user_names (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS safe_status (user_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS onboarded (user_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL)")
        c.commit()


def remember_name(db, uid: int, name: str) -> None:
    ensure(db)
    name = (name or "").strip()[:40]
    if not name:
        return
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT INTO user_names(user_id, name) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (uid, name))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def name_of(db, uid: int) -> str:
    ensure(db)
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT name FROM user_names WHERE user_id=?", (uid,)).fetchone()  # noqa: SLF001
    return r[0] if r else f"Користувач {uid}"


def members(db, uid: int) -> list[int]:
    ensure(db)
    with db._lock:  # noqa: SLF001
        return [r[0] for r in db._conn.execute("SELECT b FROM family_links WHERE a=? ORDER BY since_ts", (uid,)).fetchall()]  # noqa: SLF001


def create_invite(db, uid: int, now: int | None = None) -> str:
    ensure(db)
    now = int(now if now is not None else time.time())
    code = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
    with db._lock:  # noqa: SLF001
        db._conn.execute("DELETE FROM family_invites WHERE ts<?", (now - INVITE_TTL_S,))  # noqa: SLF001
        db._conn.execute("INSERT INTO family_invites(code, owner_id, ts) VALUES (?,?,?)", (code, uid, now))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return code


def accept_invite(db, code: str, uid: int, now: int | None = None) -> tuple[bool, str, int | None]:
    """(успіх, повідомлення, owner_id). Зв'язок взаємний."""
    ensure(db)
    now = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        row = db._conn.execute("SELECT owner_id, ts FROM family_invites WHERE code=?", (code,)).fetchone()  # noqa: SLF001
    if not row or now - row[1] > INVITE_TTL_S:
        return False, "Запрошення недійсне або застаріло (діє 48 годин). Попроси нове.", None
    owner = row[0]
    if owner == uid:
        return False, "Це твоє власне запрошення — надішли його близькій людині.", None
    if uid in members(db, owner):
        return False, "Ви вже в одній родині.", owner
    if len(members(db, owner)) >= MAX_MEMBERS or len(members(db, uid)) >= MAX_MEMBERS:
        return False, f"У родині не більше {MAX_MEMBERS} людей.", owner
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT OR IGNORE INTO family_links(a,b,since_ts) VALUES (?,?,?)", (owner, uid, now))  # noqa: SLF001
        db._conn.execute("INSERT OR IGNORE INTO family_links(a,b,since_ts) VALUES (?,?,?)", (uid, owner, now))  # noqa: SLF001
        db._conn.execute("DELETE FROM family_invites WHERE code=?", (code,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True, "ok", owner


def remove_member(db, uid: int, other: int) -> bool:
    ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM family_links WHERE (a=? AND b=?) OR (a=? AND b=?)", (uid, other, other, uid))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount > 0


def set_safe(db, uid: int, now: int | None = None) -> None:
    ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT INTO safe_status(user_id, ts) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET ts=excluded.ts", (uid, int(now if now is not None else time.time())))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def is_safe(db, uid: int, now: int | None = None) -> bool:
    ensure(db)
    now = int(now if now is not None else time.time())
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT ts FROM safe_status WHERE user_id=?", (uid,)).fetchone()  # noqa: SLF001
    return bool(r and now - r[0] <= SAFE_TTL_S)


def clear_safe(db, uid: int) -> None:
    ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("DELETE FROM safe_status WHERE user_id=?", (uid,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def needs_onboarding(db, uid: int) -> bool:
    ensure(db)
    with db._lock:  # noqa: SLF001
        return db._conn.execute("SELECT 1 FROM onboarded WHERE user_id=?", (uid,)).fetchone() is None  # noqa: SLF001


def mark_onboarded(db, uid: int) -> None:
    ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT OR IGNORE INTO onboarded(user_id, ts) VALUES (?,?)", (uid, int(time.time())))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


async def notify_members(db, send, uid: int, text: str) -> int:
    """Надіслати текст усім у родині користувача uid; send(user_id, text) — корутина."""
    n = 0
    for m in members(db, uid):
        try:
            await send(m, text)
            n += 1
        except Exception:  # noqa: BLE001 — недоступний родич не ламає решту
            pass
    return n


def danger_text(db, uid: int, label: str, risk_line: str) -> str:
    place = f" біля «{label}»" if label else ""
    return f"👪 ⚠️ {name_of(db, uid)}: небезпечно{place}\n{risk_line}"


def safe_text(db, uid: int) -> str:
    return f"👪 ✅ {name_of(db, uid)} повідомляє: в укритті"


KB_VERSION = 3   # номер розкладки нижньої клавіатури; зростає при додаванні кнопок


def kb_outdated(db, uid: int) -> bool:
    ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS kb_version (user_id INTEGER PRIMARY KEY, ver INTEGER NOT NULL)")  # noqa: SLF001
        r = db._conn.execute("SELECT ver FROM kb_version WHERE user_id=?", (uid,)).fetchone()  # noqa: SLF001
    return not r or r[0] < KB_VERSION


def set_kb_current(db, uid: int) -> None:
    ensure(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS kb_version (user_id INTEGER PRIMARY KEY, ver INTEGER NOT NULL)")  # noqa: SLF001
        db._conn.execute("INSERT INTO kb_version(user_id, ver) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET ver=excluded.ver", (uid, KB_VERSION))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
