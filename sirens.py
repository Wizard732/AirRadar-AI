"""sirens.py — официальный учёт сирен через alerts.in.ua (опционально).

API: https://api.alerts.in.ua (v1), токен выдаётся на alerts.in.ua/api.
Задаётся переменной ALERTS_IN_UA_TOKEN: пусто = модуль выключен, бот живёт
как раньше (сирены не видны). При заданном токене фоновый цикл в main.run
опросит активные тревоги и хранит состояния по регионам в siren_states.

Честность данных: это ИСТОЧНИК СИРЕН, а не замена нашей аналитики —
используется для отображения факта «тривога/нет» и будущей сверки
прогнозов с реальностью.

Схема ответа парсится толерантно: поле типа тревоги ищется среди
("alert_type", "type"), имя региона — среди ("region_name", "region_title").
Идентификатор region_id маппится по официальной таблице v1; при неизвестном
id — фолбэк по нормализованному названию области. Нераспознанные записи
не ломают парсер (debug-лог).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

API_BASE = "https://api.alerts.in.ua"

# Официальная таблица region_id → наш slug (v1, стабильна, алфавит UA).
# Київ-місто (11) и Севастополь (20) отдельных слагов в проекте не имеют:
# город Киева живёт в kyivska (+ берега), Севастополь — в crimea.
ALERTS_IN_UA_REGION_IDS: dict[int, str] = {
    1: "cherkaska",
    2: "chernigivska",
    3: "chernivetska",
    4: "crimea",
    5: "dnipropetrovska",
    6: "donetska",
    7: "ivano_frankivska",
    8: "kharkivska",
    9: "khersonska",
    10: "khmelnytska",
    11: "kyivska",
    12: "kyivska",
    13: "kirovohradska",
    14: "luhanska",
    15: "lvivska",
    16: "mykolaivska",
    17: "odeska",
    18: "poltavska",
    19: "rivnenska",
    20: "crimea",
    21: "sumska",
    22: "ternopilska",
    23: "zakarpatska",
    24: "vinnytska",
    25: "zaporizka",
    26: "zhytomyrska",
}

# Реальная схема v1 (проверено живым ответом): location_oblast_uid — официальный uid области.
LOCATION_OBLAST_UIDS: dict[int, str] = {
    3: "khmelnytska", 4: "vinnytska", 5: "rivnenska", 8: "volynska", 9: "dnipropetrovska",
    10: "zhytomyrska", 11: "zakarpatska", 12: "zaporizka", 13: "ivano_frankivska", 14: "kyivska",
    15: "kirovohradska", 16: "luhanska", 17: "mykolaivska", 18: "odeska", 19: "poltavska",
    20: "sumska", 21: "ternopilska", 22: "kharkivska", 23: "khersonska", 24: "cherkaska",
    25: "chernigivska", 26: "chernivetska", 27: "lvivska", 28: "donetska", 29: "crimea",
    30: "crimea", 31: "kyivska",
}
# Тревога региона = на уровне области/города. Районные/громадные — отдельным полем, регион целиком не «включают».
REGION_LEVEL_TYPES = {"oblast", "city"}

# Типы тревог, считаемые «сиреной». Прочие (восстановление и т.п.) — нет.
ALERT_TYPES = {"air_raid", "missile_threat"}

_TYPE_KEYS = ("alert_type", "type")
_NAME_KEYS = ("location_oblast", "location_title", "region_name", "region_title")


def _norm_name(raw: str) -> str:
    """«Харківська область» / «м. Київ» → ключ для сопоставления со slug."""
    lowered = (raw or "").strip().lower()
    for suffix in (" область", " обл.", " обл", " oblast", " city", " місто", " м."):
        lowered = lowered.replace(suffix, "")
    return lowered.strip()


# Нормализованное название → наш slug (фолбэк, если region_id неизвестен).
_NAME_TO_SLUG: dict[str, str] = {
    _norm_name(name): slug
    for slug, name in {
        "cherkaska": "Черкаська область", "chernigivska": "Чернігівська область",
        "chernivetska": "Чернівецька область", "crimea": "Крим",
        "dnipropetrovska": "Дніпропетровська область", "donetska": "Донецька область",
        "ivano_frankivska": "Івано-Франківська область", "kharkivska": "Харківська область",
        "khersonska": "Херсонська область", "khmelnytska": "Хмельницька область",
        "kyivska": "Київська область", "volynska": "Волинська область",
        "kirovohradska": "Кіровоградська область", "luhanska": "Луганська область",
        "lvivska": "Львівська область", "mykolaivska": "Миколаївська область",
        "odeska": "Одеська область", "poltavska": "Полтавська область",
        "rivnenska": "Рівненська область",
        "sumska": "Сумська область", "ternopilska": "Тернопільська область",
        "zakarpatska": "Закарпатська область", "vinnytska": "Вінницька область",
        "zaporizka": "Запорізька область", "zhytomyrska": "Житомирська область",
    }.items()
}


def _entry_slug(entry: dict[str, Any]) -> str:
    """Slug региона из одной записи active-alerts ('' — не распознали)."""
    uid = entry.get("location_oblast_uid")
    slug = LOCATION_OBLAST_UIDS.get(uid) if isinstance(uid, int) else ""
    region_id = entry.get("region_id")
    if not slug:
        slug = ALERTS_IN_UA_REGION_IDS.get(region_id) if isinstance(region_id, int) else ""
    if not slug:
        for key in _NAME_KEYS:
            slug = _NAME_TO_SLUG.get(_norm_name(str(entry.get(key) or "")), "")
            if slug:
                break
    return slug


def active_alert_slugs(payload: dict[str, Any]) -> set[str]:
    """Слуги регионов с активной сиреной из ответа /v1/alerts/active.json.

    Толерантно к схеме: неизвестные записи пропускаются с debug-логом,
    отсутствующий или пустой alerts[] даёт пустое множество (не ошибку).
    """
    out: set[str] = set()
    entries = payload.get("alerts") if isinstance(payload, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        alert_type = ""
        for key in _TYPE_KEYS:
            alert_type = str(entry.get(key) or "").strip().lower()
            if alert_type:
                break
        if alert_type and alert_type not in ALERT_TYPES:
            continue  # не сирена (восстановление/прочее)
        loc_type = str(entry.get("location_type") or "").lower()
        if loc_type and loc_type not in REGION_LEVEL_TYPES:
            continue  # район/громада: область цілком не під тривогою
        if loc_type == "city" and entry.get("location_oblast_uid") not in (30, 31):
            continue  # місто в області (не Київ/Севастополь) — це не вся область
        slug = _entry_slug(entry)
        if slug:
            out.add(slug)
        else:
            logger.debug("sirens: нераспознанная запись тревоги: %r", entry)
    return out


def raion_key(entry: dict[str, Any]) -> str:
    """«Мелітопольський район», «Запорізька область» → 'Мелітопольський|Запорізька' (ключ полігона району на карті)."""
    raion = str(entry.get("location_title") or "").replace(" район", "").strip()
    oblast = str(entry.get("location_oblast") or "").split(" ")[0].strip()
    return f"{raion}|{oblast}" if raion and oblast else ""


def active_raion_keys(payload: dict[str, Any]) -> set[str]:
    """Райони з активною сиреною (location_type=raion). Для підсвітки на карті; регіон цілком не вмикають."""
    out: set[str] = set()
    for entry in (payload.get("alerts") if isinstance(payload, dict) else None) or []:
        if isinstance(entry, dict) and str(entry.get("location_type") or "").lower() == "raion" \
                and str(entry.get("alert_type") or "air_raid").lower() in ALERT_TYPES:
            key = raion_key(entry)
            if key:
                # red — діє тривога; yellow — попереджувальний рівень (загроза поруч), як жовті райони на мапах
                out.add(("raionY:" if str(entry.get("alert_level") or "").lower() == "yellow" else "raion:") + key)
    return out


def active_extra_keys(payload: dict[str, Any]) -> set[str]:
    """Додаткові ключі для карти: громади → «raionP:» (район частково під тривогою), типи загроз → «threat:slug:тип»."""
    out: set[str] = set()
    for entry in (payload.get("alerts") if isinstance(payload, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        loc_type = str(entry.get("location_type") or "").lower()
        if loc_type == "hromada" and str(entry.get("alert_type") or "air_raid").lower() in ALERT_TYPES:
            raion = str(entry.get("location_raion") or "").replace(" район", "").strip()
            oblast = str(entry.get("location_oblast") or "").split(" ")[0].strip()
            if raion and oblast:
                out.add(f"raionP:{raion}|{oblast}")
        slug = _entry_slug(entry)
        for th in entry.get("threats") or []:
            kind = str((th or {}).get("threat_type") or "").strip()
            if slug and kind:
                out.add(f"threat:{slug}:{kind}")
    return out


def alert_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Короткий список активних тривог для листа «Область → райони» на карті (без зайвих полів)."""
    out = []
    for e in (payload.get("alerts") if isinstance(payload, dict) else None) or []:
        if not isinstance(e, dict):
            continue
        slug = _entry_slug(e)
        if not slug:
            continue
        out.append({
            "slug": slug, "type": str(e.get("location_type") or ""), "title": str(e.get("location_title") or ""),
            "raion": str(e.get("location_raion") or ""), "level": str(e.get("alert_level") or "red"),
            "kind": str(e.get("alert_type") or ""), "since": str(e.get("started_at") or ""),
            "threats": [str((t or {}).get("threat_type") or "") for t in (e.get("threats") or [])],
        })
    return out


async def fetch_active_alerts(
    session: aiohttp.ClientSession, token: str, timeout_sec: int = 15,
) -> dict[str, Any] | None:
    """GET /v1/alerts/active.json с Bearer-токеном; None при любой ошибке."""
    if not token:
        return None
    try:
        async with session.get(
            f"{API_BASE}/v1/alerts/active.json",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                logger.warning("alerts.in.ua HTTP %s — пропускаю опрос", resp.status)
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.debug("alerts.in.ua недоступен: %s", exc)
        return None
