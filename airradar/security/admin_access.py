"""admin_access.py — безпечна видача/відкликання адмін-прав з аудитом.

Принципи:
  * право визначається ТІЛЬКИ числовим Telegram ID; відображуване ім'я/@username ніколи
    не дає прав саме по собі — username лише резолвиться в ID, який адмін підтверджує вручну;
  * видача двокроковa: запит → одноразовий токен (TTL, прив'язаний до видавця й цілі) → підтвердження;
  * токен одноразовий: після використання/протермінування повторно не діє;
  * мінімальні права: видавати/забирати може лише супер-адмін, звичайний адмін — тільки діагностика;
  * кожна дія (успішна чи відхилена) пишеться в admin_audit; секрети в журнал не потрапляють.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

TOKEN_TTL_S = 120
_MAX_DETAIL = 300

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        actor_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_id INTEGER,
        ok INTEGER NOT NULL,
        detail TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit(ts)",
    """CREATE TABLE IF NOT EXISTS admin_tokens (
        token_hash TEXT PRIMARY KEY,
        actor_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        expires_ts INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    )""",
)


def ensure_schema(db) -> None:
    done = db.__dict__.setdefault("_ar_schema_done", set())  # DDL раз на екземпляр БД, а не на кожен виклик
    if "admin" in done:
        return
    with db._lock:  # noqa: SLF001
        for sql in _SCHEMA:
            db._conn.execute(sql)  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    done.add("admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def log_action(db, actor_id: int, action: str, target_id: int | None = None, ok: bool = True,
               detail: str = "", now: int | None = None) -> None:
    """Записати адміністративну дію. Ніколи не кидає виняток (аудит не має ламати бота)."""
    try:
        ensure_schema(db)
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                "INSERT INTO admin_audit (ts, actor_id, action, target_id, ok, detail) VALUES (?,?,?,?,?,?)",
                ((int(time.time()) if now is None else now), int(actor_id), action, target_id, 1 if ok else 0, (detail or "")[:_MAX_DETAIL]),
            )
            db._conn.commit()  # noqa: SLF001
    except Exception:  # noqa: BLE001 — аудит не має ламати основний потік
        pass


def recent_audit(db, limit: int = 20) -> list[dict]:
    ensure_schema(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT ts, actor_id, action, target_id, ok, detail FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def request_grant(db, actor_id: int, super_admin_id: int, target_id: int, now: int | None = None) -> str | None:
    """Крок 1: створити одноразовий токен підтвердження. None — якщо в актора немає права."""
    now = (int(time.time()) if now is None else now)
    if actor_id != super_admin_id or not super_admin_id:
        log_action(db, actor_id, "grant_request", target_id, False, "not_super_admin", now)
        return None
    if not isinstance(target_id, int) or target_id <= 0 or target_id == super_admin_id:
        log_action(db, actor_id, "grant_request", target_id, False, "invalid_target", now)
        return None
    ensure_schema(db)
    token = secrets.token_urlsafe(9)
    with db._lock:  # noqa: SLF001
        db._conn.execute("DELETE FROM admin_tokens WHERE expires_ts < ? OR used = 1", (now,))  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "INSERT INTO admin_tokens (token_hash, actor_id, target_id, action, expires_ts) VALUES (?,?,?,?,?)",
            (_hash(token), actor_id, target_id, "grant", now + TOKEN_TTL_S),
        )
        db._conn.commit()  # noqa: SLF001
    log_action(db, actor_id, "grant_request", target_id, True, "token_issued", now)
    return token


def confirm_grant(db, actor_id: int, super_admin_id: int, token: str, now: int | None = None) -> int | None:
    """Крок 2: спожити токен і видати права. Повертає ID отримувача або None."""
    now = (int(time.time()) if now is None else now)
    if actor_id != super_admin_id or not super_admin_id:
        log_action(db, actor_id, "grant_confirm", None, False, "not_super_admin", now)
        return None
    ensure_schema(db)
    h = _hash(token or "")
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(  # noqa: SLF001
            "SELECT token_hash, actor_id, target_id, expires_ts, used FROM admin_tokens WHERE token_hash = ?", (h,)
        ).fetchone()
        valid = (row is not None and hmac.compare_digest(row["token_hash"], h) and not row["used"]
                 and row["actor_id"] == actor_id and row["expires_ts"] >= now)
        if valid:
            # атомарне «спалювання»: другий виклик з тим самим токеном отримає rowcount 0
            cur = db._conn.execute("UPDATE admin_tokens SET used = 1 WHERE token_hash = ? AND used = 0", (h,))  # noqa: SLF001
            valid = cur.rowcount == 1
        db._conn.commit()  # noqa: SLF001
    if not valid:
        log_action(db, actor_id, "grant_confirm", None, False, "bad_expired_or_reused_token", now)
        return None
    target = int(row["target_id"])
    db.add_admin(target, actor_id)
    log_action(db, actor_id, "grant_admin", target, True, "confirmed", now)
    return target


def revoke(db, actor_id: int, super_admin_id: int, target_id: int, now: int | None = None) -> bool:
    if actor_id != super_admin_id or not super_admin_id:
        log_action(db, actor_id, "revoke_admin", target_id, False, "not_super_admin", now)
        return False
    if target_id == super_admin_id:
        log_action(db, actor_id, "revoke_admin", target_id, False, "cannot_revoke_super", now)
        return False
    db.remove_admin(target_id)
    log_action(db, actor_id, "revoke_admin", target_id, True, "", now)
    return True
