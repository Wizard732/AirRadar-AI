"""push_fcm.py — push-сповіщення в мобільний додаток (Android + iOS) через Firebase Cloud Messaging.

Без вхідних портів: додаток САМ підписується на FCM-тему u_<код> (код він генерує локально) і показує його користувачеві;
користувач надсилає боту `/pair <код>` — так сервер дізнається тему. Далі сервер лише ВІДПРАВЛЯЄ пуші в FCM (вихідні запити).
Секретність теми = секретність коду (16 випадкових символів), тому код варто показувати лише в самому додатку.

Налаштування (.env): FCM_SERVICE_ACCOUNT=/шлях/до/service-account.json (ключ Firebase → Project settings → Service accounts).
Без нього модуль «спить»: configured() == False, нічого не надсилається й нічого не ламається.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[a-z0-9]{12,32}$")
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
ANDROID_CHANNELS = {"danger": "airradar_danger", "alarm": "airradar_alarm", "warn": "airradar_warn", "info": "airradar_info"}
_token_cache: dict = {"token": "", "exp": 0.0, "project": ""}


def service_account_path() -> str:
    return os.getenv("FCM_SERVICE_ACCOUNT", "").strip()


NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_PRIORITY = {"danger": 5, "alarm": 5, "warn": 4, "info": 3}
NTFY_TAGS = {"danger": ["rotating_light", "danger"], "alarm": ["rotating_light"], "warn": ["warning"], "info": ["information_source"]}


def fcm_configured() -> bool:
    p = service_account_path()
    return bool(p) and Path(p).exists()


def ntfy_enabled() -> bool:
    return os.getenv("NTFY", "1").strip() not in ("0", "false", "no")


def configured() -> bool:
    """Є хоча б один канал доставки: ntfy (безкоштовно, за замовчуванням) або FCM (потрібен Firebase)."""
    return ntfy_enabled() or fcm_configured()


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS device_topics (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "  # noqa: SLF001
                         "topic TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL)")
        db._conn.execute("CREATE INDEX IF NOT EXISTS idx_device_user ON device_topics(user_id)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def pair(db, uid: int, code: str, max_devices: int = 5) -> tuple[bool, str]:
    """Прив'язати код додатка до користувача Telegram."""
    ensure(db)
    code = code.strip().lower()
    if not CODE_RE.match(code):
        return False, "Код має складатися з 12–32 латинських літер і цифр — скопіюй його з додатка."
    topic = f"ar-{code}"   # застосунок слухає ту саму ntfy-тему, що й сторонні клієнти ntfy
    with db._lock:  # noqa: SLF001
        row = db._conn.execute("SELECT user_id FROM device_topics WHERE topic=?", (topic,)).fetchone()  # noqa: SLF001
        if row and row[0] != uid:
            return False, "Цей код уже прив'язаний до іншого акаунта. Згенеруй новий у додатку."
        n = db._conn.execute("SELECT COUNT(*) FROM device_topics WHERE user_id=?", (uid,)).fetchone()[0]  # noqa: SLF001
        if not row and n >= max_devices:
            return False, f"До {max_devices} пристроїв на акаунт. Відключи зайвий: /unpair"
        db._conn.execute("INSERT OR IGNORE INTO device_topics(user_id, topic, ts) VALUES (?,?,?)", (uid, topic, int(time.time())))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True, topic


def ntfy_topic_for(db, uid: int) -> str:
    """Тема ntfy користувача (створюється при першому зверненні). Секретність = випадкові 24 hex-символи."""
    import secrets
    ensure(db)
    for t in topics_of(db, uid):
        if t.startswith("ar-"):
            return t
    topic = "ar-" + secrets.token_hex(12)
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT INTO device_topics(user_id, topic, ts) VALUES (?,?,?)", (uid, topic, int(time.time())))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return topic


def unpair_all(db, uid: int) -> int:
    ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM device_topics WHERE user_id=?", (uid,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount


def topics_of(db, uid: int) -> list[str]:
    ensure(db)
    with db._lock:  # noqa: SLF001
        return [r[0] for r in db._conn.execute("SELECT topic FROM device_topics WHERE user_id=?", (uid,)).fetchall()]  # noqa: SLF001


def build_message(topic: str, title: str, body: str, level: str = "info", data: dict | None = None) -> dict:
    """FCM v1 повідомлення. level: alarm (гучний канал + тривожний звук) | warn | info."""
    loud = level in ("danger", "alarm", "warn")
    return {"message": {
        "topic": topic,
        "notification": {"title": title[:80], "body": body[:400]},
        "data": {k: str(v) for k, v in {"level": level, **(data or {})}.items()},
        "android": {"priority": "HIGH", "ttl": "300s",
                    "notification": {"channel_id": ANDROID_CHANNELS.get(level, "airradar_info"),
                                     "sound": "danger_beeps" if level == "danger" else "alarm" if level == "alarm" else "default",
                                     "default_vibrate_timings": True, "notification_priority": "PRIORITY_MAX" if loud else "PRIORITY_DEFAULT"}},
        "apns": {"headers": {"apns-priority": "10" if loud else "5", "apns-expiration": str(int(time.time()) + 300)},
                 "payload": {"aps": {"sound": "alarm.caf" if level == "alarm" else "default",
                                     "interruption-level": "time-sensitive" if loud else "active"}}},
    }}


def _access_token() -> tuple[str, str]:
    """(token, project_id). Блокуюче — викликати через executor."""
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] - 60 > now:
        return _token_cache["token"], _token_cache["project"]
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    path = service_account_path()
    creds = service_account.Credentials.from_service_account_file(path, scopes=[SCOPE])
    creds.refresh(Request())
    project = json.loads(Path(path).read_text()).get("project_id", "")
    _token_cache.update(token=creds.token, exp=creds.expiry.timestamp() if creds.expiry else now + 1800, project=project)
    return creds.token, project


async def _post(message: dict) -> tuple[int, str]:
    import aiohttp
    loop = asyncio.get_running_loop()
    token, project = await loop.run_in_executor(None, _access_token)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send", json=message,
                          headers={"Authorization": f"Bearer {token}"}) as r:
            return r.status, (await r.text())[:300]


def build_ntfy(topic: str, title: str, body: str, level: str = "info", click: str = "") -> dict:
    out = {"topic": topic, "title": title[:100], "message": body[:1000], "priority": NTFY_PRIORITY.get(level, 3),
           "tags": NTFY_TAGS.get(level, [])}
    if click:
        out["click"] = click
    return out


async def _post_ntfy(payload: dict) -> tuple[int, str]:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(NTFY_URL + "/", json=payload) as r:
            return r.status, (await r.text())[:200]


async def send_topic(topic: str, title: str, body: str, level: str = "info", data: dict | None = None) -> bool:
    if topic.startswith("ar-"):
        if not ntfy_enabled():
            return False
        try:
            status, text = await _post_ntfy(build_ntfy(topic, title, body, level, (data or {}).get("click", "")))
        except Exception:  # noqa: BLE001
            logger.warning("ntfy: помилка відправки", exc_info=True)
            return False
        if status != 200:
            logger.warning("ntfy %s: %s", status, text)
        return status == 200
    if not fcm_configured():
        return False
    try:
        status, text = await _post(build_message(topic, title, body, level, data))
    except Exception:  # noqa: BLE001 — пуш не повинен ламати основний потік
        logger.warning("FCM: помилка відправки", exc_info=True)
        return False
    if status != 200:
        logger.warning("FCM %s: %s", status, text)
    return status == 200


async def push_user(db, uid: int, title: str, body: str, level: str = "info", data: dict | None = None) -> int:
    """Пуш на всі прив'язані пристрої користувача. Повертає кількість успішних."""
    if not configured():
        return 0
    ok = 0
    for topic in topics_of(db, uid):
        ok += 1 if await send_topic(topic, title, body, level, data) else 0
    return ok


async def push_regions(db, regions: list[str], title: str, body: str, level: str = "info") -> int:
    """Пуш усім підписникам областей, у кого є прив'язаний додаток (унікально)."""
    if not configured():
        return 0
    seen: set[int] = set()
    n = 0
    for slug in regions:
        for uid in db.get_subscribers(slug):
            if uid in seen:
                continue
            seen.add(uid)
            n += await push_user(db, uid, title, body, level)
    return n
