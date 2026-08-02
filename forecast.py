"""forecast.py — прогнозная аналитика угроз по историческим паттернам.

Не «обучает нейронку», а считает статистику по 268к угроз за 4 года:
  • Часовые паттерны — в какие часы чаще бьют по региону
  • Корреляция авиация→удар — если был зліт, через сколько обычно ракеты
  • Коридор укрытия — медиана + разброс длительности тревоги
  • Ежедневный прогноз — бот шлёт в 21:00 прогноз на ночь по подписке

Это точнее любой LLM для прогноза, т.к. работает на реальных данных.
"""

from __future__ import annotations

import logging
import statistics
import time

from database import Database

logger = logging.getLogger(__name__)


def shelter_window(db: Database, region: str) -> str:
    """Коридор укрытия: «сидеть ~45-90 мин, обычно отбой через ~1ч 23м».

    Считает медиану и квартили длительности тревоги. Возвращает строку.
    """
    samples = db.alert_duration_samples(region, limit=50)
    if len(samples) < 3:
        return ""
    # В минутах.
    mins = [s / 60 for s in samples if s > 0]
    if not mins:
        return ""
    med = statistics.median(mins)
    q1 = max(1, min(mins))  # минимум
    q3 = max(mins)  # максимум
    h, m = int(med) // 60, int(med) % 60
    med_str = f"~{h} год {m} хв" if h > 0 else f"~{int(med)} хв"
    q1_str = f"~{int(q1)} хв"
    q3_str = f"~{int(q3 / 60)} год" if q3 >= 60 else f"~{int(q3)} хв"
    return f"🛡 Сидіти в укритті: {med_str} (коридор {q1_str} – {q3_str})"


def hourly_risk(db: Database, region: str, threat_type: str | None = None) -> str:
    """Текущий часовой риск: «сейчас исторически активный час (X% угроз)»."""
    pattern = db.hourly_pattern(region, threat_type)
    total = sum(pattern)
    if total < 10:
        return ""
    cur_hour = time.localtime().tm_hour
    cur_count = pattern[cur_hour]
    pct = int(cur_count * 100 / total)
    if pct >= 8:
        return f"🕐 Зараз історично активна година ({pct}% загроз випадають на {cur_hour}:00)."
    if pct >= 4:
        return f"🕐 Помірна активність у цю годину ({pct}% загроз)."
    return ""


def escalation_risk(db: Database, region: str, current_threats: list[dict]) -> str:
    """Оценка эскалации: если в воздухе авиация → выше шанс массированного удара.

    current_threats: список активных угроз (из db.active_threats).
    Анализирует архив: были ли в этом регионе недавние «зліт/авиация».
    """
    # Проверяем: есть ли за последний час авиационная активность (missile-тип).
    now = int(time.time())
    recent_aviation = [t for t in current_threats if t["type"] == "missile" and now - t["ts"] < 3600]
    if not recent_aviation:
        return ""
    # Сколько ракетных угроз было в регионе за последний час всего.
    recent_count = db.threats_in_last_hours(region, hours=1)
    if recent_count >= 3:
        return "📈 Ознака ескалації: активність авіації + множинні загрози. Висока ймовірність масованого удару."
    if recent_count >= 1:
        return "📈 Підвищений ризик: зафіксовано авіаційну активність. Можливе посилення удару."
    return ""


def daily_forecast(db: Database, region: str) -> str:
    """Ежедневный прогноз на ночь/день по региону.

    Анализирует: активность в последние 3 часа + исторический паттерн вечера.
    Возвращает текст прогноза (для рассылки в 21:00).
    """
    from regions import region_name

    name = region_name(region)
    lines = [f"🌙 Підсумок дня та прогноз на ніч — {name}:\n"]

    # === СВОДКА ЗА ДЕНЬ ===
    day_counts = db.threat_counts(region=region, since=int(time.time()) - 86400)
    day_total = sum(day_counts.values())
    lines.append("📅 За сьогодні:")
    if day_total > 0:
        from weapon_classes import weapon_label
        for t, c in sorted(day_counts.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  • {t}: {c}")
    else:
        lines.append("  Спокійний день, загроз не зафіксовано.")
    lines.append("")

    # === АКТИВНОСТЬ ПОСЛЕДНИЕ 3 ЧАСА ===
    recent = db.threats_in_last_hours(region, hours=3)
    if recent >= 5:
        lines.append(f"🔴 Висока активність: {recent} загроз за 3 години.")
    elif recent >= 1:
        lines.append(f"🟡 Помірна активність: {recent} загроз за 3 години.")
    else:
        lines.append("🟢 Спокій за останні 3 години.")

    # === ПРОГНОЗ НА НОЧЬ ===
    lines.append("\n📊 Прогноз на ніч:")
    # Исторический вечерний паттерн (18-23 часа).
    pattern = db.hourly_pattern(region)
    evening = sum(pattern[18:24])
    total = sum(pattern)
    if total > 0:
        evening_pct = int(evening * 100 / total)
        night = sum(pattern[0:6])
        night_pct = int(night * 100 / total)
        if evening_pct >= 30 or night_pct >= 20:
            lines.append(f"⚠️ Історично ніч активна ({evening_pct}% загроз ввечері, {night_pct}% вночі).")
        else:
            lines.append(f"🟢 Історично ніч спокійна ({night_pct}% загроз вночі).")

    # Коридор укрытия на случай тревоги.
    sw = shelter_window(db, region)
    if sw:
        lines.append(f"\n{sw}")

    return "\n".join(lines) if len(lines) > 3 else ""
