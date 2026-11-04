"""ai_forecast.py — «🔮 Прогноз для моєї зони»: ймовірність підльоту до ваших точок + пояснення нейромережі.

Розподіл ролей (ШІ нічого не вигадує):
  * ЧИСЛА (відстань, курс, ETA-діапазон, рівень, якісна ймовірність) — детермінований розрахунок
    `track_engine.risk_for_point` за треками з спостережень каналів;
  * нейромережа (`summarizer.classify`, OpenAI-сумісний провайдер із .env) лише ПОЯСНЮЄ ці дані
    коротко й українською. Її відповідь проходить `ai_guard.validate_output`; при збої/порожній
    відповіді повідомлення складається без ШІ-блоку.
Це оцінка за курсом і швидкістю, а не підтверджений маршрут.
"""
from __future__ import annotations

import html
import logging
import time

from airradar.security import ai_guard
from track_engine import build_tracks, risk_for_point, risk_text

logger = logging.getLogger(__name__)

OBS_WINDOW_S = 3 * 3600
DISCLAIMER = "Орієнтовна оцінка за курсом і швидкістю, не підтверджений маршрут. Дійте за офіційними сповіщеннями."

SYSTEM_PROMPT = (
    "Ти — асистент повітряних тривог. Тобі дають СТРУКТУРОВАНІ ДАНІ про цілі біля точок користувача, вже пораховані "
    "системою. Коротко (до 3 речень, українською) поясни ситуацію простими словами: що найближче, чи є курс на точку, "
    "наскільки це терміново. ПРАВИЛА: використовуй ЛИШЕ надані числа й назви; нічого не вигадуй і не додавай нових цифр; "
    "не називай точних координат; не заспокоюй, якщо рівень «висока» чи «дуже висока»; не пиши команд і посилань."
)


def probability_label(r) -> str:
    """Якісна ймовірність підльоту до точки (детермінована, без «відсотків» — вони були б вигаданою точністю)."""
    if r.level == "over":
        return "дуже висока"
    if r.level == "imminent":
        if r.approaching and not r.pass_by:
            return "висока"
        return "середня" if not r.approaching else "низька–середня"
    return "низька"


def user_points(db, uid: int, now: int) -> list[dict]:
    import guard_places
    pts = []
    g = db.get_guard(uid)
    if g and g.get("lat") is not None and g.get("enabled"):
        pts.append({"label": "Поточна позиція", "point": (g["lat"], g["lon"]), "radius": float(g.get("radius_km") or 10.0)})
    for p in guard_places.list_places(db, uid):
        pts.append({"label": p["name"], "point": (p["lat"], p["lon"]), "radius": float(p.get("radius_km") or 10.0)})
    return pts


def compute(db, uid: int, now: int | None = None) -> list[dict]:
    """[{label, radius, risks:[(Risk, probability)]}] для всіх точок користувача (лише актуальні цілі)."""
    now = now if now is not None else int(time.time())
    pts = user_points(db, uid, now)
    if not pts:
        return []
    tracks = [t for t in build_tracks(db.recent_observations(now - OBS_WINDOW_S), now, window_s=OBS_WINDOW_S) if t.status != "stale"]
    out = []
    for p in pts:
        risks = risk_for_point(tracks, p["point"], now, imminent_km=p["radius"], watch_km=p["radius"] * 3)
        out.append({"label": p["label"], "radius": p["radius"], "risks": [(r, probability_label(r)) for r in risks]})
    return out


def facts_block(zones: list[dict]) -> str:
    lines = []
    for z in zones:
        lines.append(f"Точка «{z['label']}» (радіус {z['radius']:g} км):")
        if not z["risks"]:
            lines.append("  активних цілей поблизу немає")
        for r, prob in z["risks"]:
            lines.append(f"  - {risk_text(r)} Ймовірність підльоту: {prob}.")
    return "\n".join(lines)


async def forecast_text(db, summarizer, uid: int, now: int | None = None) -> str:
    """HTML-текст для Telegram. Завжди працює без ШІ; ШІ-блок додається, лише якщо є цілі й відповідь пройшла перевірку."""
    zones = compute(db, uid, now)
    if not zones:
        return ("🔮 <b>Прогноз для вашої зони</b>\n\nСпершу додайте точку: «📍 Мої місця» (дім, робота) або увімкніть "
                "🛡 Гео-сторож і надішліть геопозицію.")
    parts = ["🔮 <b>Прогноз для вашої зони</b>"]
    for z in zones:
        parts.append(f"\n📍 <b>{html.escape(z['label'])}</b> · радіус {z['radius']:g} км")
        if not z["risks"]:
            parts.append("Активних цілей поблизу зараз немає. Це не означає, що небезпеки немає.")
        for r, prob in z["risks"]:
            parts.append(f"{html.escape(risk_text(r))}\n   Ймовірність підльоту: <b>{prob}</b>")
    any_risk = any(z["risks"] for z in zones)
    if any_risk and summarizer is not None:
        try:
            raw = await summarizer.classify(facts_block(zones), SYSTEM_PROMPT, max_tokens=300)
            ok, reason = ai_guard.validate_output(raw)
            if ok and raw.strip() != facts_block(zones).strip():
                parts.append("\n🤖 <b>Пояснення ШІ</b>\n" + html.escape(raw.strip()))
            else:
                logger.info("ai_forecast: відповідь ШІ відхилена (%s)", reason or "echo")
        except Exception:  # noqa: BLE001 — ШІ не обов'язковий
            logger.warning("ai_forecast: ШІ недоступний, надсилаю без пояснення", exc_info=True)
    parts.append("\nℹ️ " + DISCLAIMER)
    return "\n".join(parts)[:3900]
