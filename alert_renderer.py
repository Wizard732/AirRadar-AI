"""Evidence-first public alerts: direct source facts, never forecasts.

Компактный формат публичного поста:
    🔴 Київ та область | БПЛА
    🕒 09:21
    📍 Рух: цілі прямують з Сумська обл. на Київ та область   (если известен маршрут)
    ⚠️ Статус: підтверджено 2 незалежними джерелами
    <текст источника, ≤300 знаков>
    📡 Джерело: AirRadar AI   (Publisher делает эту строку кликабельной ссылкой)

Без ETA-прогнозов и вероятностей. Только прямые факты источника. Имена
исходных каналов в пост НЕ попадают: строка «Джерело» — это наш канал
(пассивный брендинг), а кнопка подписки прикрепляется Publisher'ом.
"""

from __future__ import annotations

import time

from incident_fusion import IncidentFact
from regions import region_name
from weapon_classes import weapon_severity

# Короткие UA-названия классов оружия для публичных постов (без эмодзи-
# заголовков weapon_label — эмодзи задаётся отдельно по критичности).
WEAPON_SHORT: dict[str, str] = {
    "fpv": "БПЛА (FPV)",
    "shahed": "БПЛА (Shahed)",
    "recon_drone": "БПЛА (розвідка)",
    "uav": "БПЛА",
    "ballistic": "Балістика",
    "cruise_missile": "Крилаті ракети",
    "air_missile": "Авіаційні ракети",
    "coastal_missile": "Берегові ракети",
    "kab": "КАБ",
    "tac_aviation": "Тактична авіація",
    "strat_aviation": "Стратегічна авіація",
    "aviation": "Авіація",
    "mlrs": "РСЗО",
    "artillery": "Артилерія",
    "explosion": "Прильот/вибух",
    "air_defense": "Робота ППО",
    "decoy": "Хибна ціль",
    "alert": "Тривога",
    "stand_down": "Відбій",
}

# Эмодзи по базовой критичности класса оружия.
_SEVERITY_EMOJI: dict[str, str] = {
    "CRITICAL": "🔴",
    "HIGH": "🔴",
    "MODERATE": "🟡",
    "LOW": "⚪",
    "ALL_CLEAR": "🟢",
}

# Строка источника в посте: всегда наш канал (пассивный брендинг вместо
# упоминания исходного канала мониторинга). Кнопка «Підписатися» вешается
# Publisher'ом (см. publisher.py, promo_url).
SOURCE_BRAND_LINE = "📡 Джерело: AirRadar AI"


def _status(confirmation: dict) -> str:
    status = confirmation.get("status", "reported")
    if status == "officially_confirmed":
        return "підтверджено офіційним джерелом"
    if status == "corroborated":
        return f"підтверджено {confirmation.get('sources', 2)} незалежними джерелами"
    return "повідомлення одного джерела"


def _region_title(fact: IncidentFact) -> str:
    """Регион для заголовка; 'unknown'/пустой не показываем."""
    region = fact.destination_region
    if region and region != "unknown":
        return region_name(region)
    return ""


def render_evidence_alert(
    *, text: str, source: str, event_ts: int, fact: IncidentFact, confirmation: dict,
    sources: list[str] | None = None,
) -> str:
    """Render only explicit source facts and clearly marked unknowns.

    sources: список уникальных источников набора (агрегатор). В пост не
    выводится — источники учитываются только в строке статуса и в БД.

    Формат с «воздухом»: пустые строки между блоками (заголовок / время и
    маршрут / статус / текст источника / бренд) — пост читается с одного
    взгляда в ленте. Заголовок капсом — визуальный якорь.
    """
    when = time.strftime("%H:%M", time.localtime(event_ts))

    if fact.weapon_class == "stand_down":
        # Отбой — отдельная короткая ветка, публикуется всегда и мгновенно.
        place = _region_title(fact)
        head = "🟢 ВІДБІЙ" + (f" — {place.upper()}" if place else "") + f" — {when}"
        return "\n\n".join([head, SOURCE_BRAND_LINE])[:4000]

    weapon = WEAPON_SHORT.get(fact.weapon_class, fact.weapon_class)
    emoji = _SEVERITY_EMOJI.get(weapon_severity(fact.weapon_class), "⚪")
    place = _region_title(fact)
    head = f"{emoji} {place.upper()} | {weapon}" if place else f"{emoji} {weapon}"
    lines = [head, ""]
    lines.append(f"🕒 {when}")
    if fact.origin_region and fact.destination_region and fact.destination_region != "unknown":
        lines.append(
            f"📍 Рух: цілі прямують з {region_name(fact.origin_region)} "
            f"на {region_name(fact.destination_region)}"
        )
    lines.append("")
    lines.append(f"⚠️ Статус: {_status(confirmation)}")
    # Текст источника — недоверенные данные: без разметки, но с сохранением
    # авторских переносов строк (сводки вида «Київщина \n 1 БпЛА на Димер»
    # схлопывать в одну строку нечитаемо).
    body = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    lines.append("")
    lines.append(body[:300])
    lines.append("")
    lines.append(SOURCE_BRAND_LINE)
    return "\n".join(lines)[:4000]
