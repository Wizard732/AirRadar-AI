"""analytics.py — обогащённый формат публикации угрозы (как в референс-боте).

Собирает из БД контекст по региону и формирует осмысленное сообщение:
  • Уровень критичности (CRITICAL/HIGH/MODERATE) по типу угрозы
  • ETA с разбросом (мин–макс) по историческим парам
  • История прильотов в регионе (были ли раньше, куда падало)
  • Средняя длительность тревоги
  • Рекомендация по укрытию

Используется в военном конвейере (main._process_message) вместо «сухого»
эмодзи+сжатия. Аналитика берётся из импортированной 4-летней истории.
"""

from __future__ import annotations

import time
from datetime import datetime

from database import Database
from forecast import escalation_risk, hourly_risk, shelter_window
from weapon_classes import classify_weapon, weapon_eta, weapon_label, weapon_severity
from eta import estimate_eta
from regions import detect_region, region_name
from sticker import classify_threat

# Уровни критичности по типу угрозы.
SEVERITY = {
    "missile": ("🔴", "CRITICAL", "НЕМЕДЛЕННО в укрытие! Ракетный удар."),
    "uav": ("🟠", "HIGH", "Угроза БПЛА. Немедленно в укрытие."),
    "artillery": ("🔴", "CRITICAL", "Обстрел! НЕМЕДЛЕННО в укрытие."),
    "explosion": ("🔴", "CRITICAL", "Взрывы! Оставаться в укрытии, не выходить."),
    "alert": ("🟡", "MODERATE", "Воздушная тревога. Готовиться к укрытию."),
    "stand_down": ("🟢", "ALL CLEAR", "Отбой. Можно выходить."),
    "other": ("🟡", "MODERATE", "Оперативная информация. Будьте начеку."),
}

# Локализованные названия типов для заголовка.
TYPE_TITLE = {
    "missile": "Ракетна загроза",
    "uav": "БПЛА (Shahed/дрон)",
    "artillery": "Артилерійський обстріл",
    "explosion": "Прильот / вибухи",
    "alert": "Повітряна тривога",
    "stand_down": "Відбій тривоги",
    "other": "Операційна інформація",
}


def build_rich_alert(
    text: str,
    summary: str,
    source: str,
    db: Database,
) -> str:
    """Собрать обогащённое сообщение об угрозе.

    text: исходный текст поста (для классификации и региона).
    summary: сжатый текст от LLM.
    source: @username канала-источника.
    db: база для аналитики (ETA, история прильотов, длительность тревоги).
    """
    # Новая классификация: полный класс оружия (не старый missile/uav).
    weapon = classify_weapon(text)
    # Регион: из текста, fallback на название канала.
    regions = detect_region(text, channel=str(source))

    # Определяем стадию угрозы по контексту.
    stage = _detect_stage(text, weapon)

    # Если стадия 'past' (упал/взорвался) — меняем класс на explosion.
    if stage == "past" and weapon not in ("explosion", "air_defense", "stand_down", "alert"):
        weapon = "explosion"

    # Критичность из класса оружия + стадия.
    base_sev = weapon_severity(weapon)
    emoji, level, recommendation = _severity_for_class(base_sev, stage)
    title = weapon_label(weapon)

    lines: list[str] = []
    lines.append(f"{emoji} {level}: {title}")

    if regions:
        lines.append(f"📍 Регіон: {region_name(regions[0])}")

    # ETA — для классов с оружием в полёте, только imminent/unknown.
    # Для быстрых классов (балістика/гіперзвук/КАБ/РСЗО/артилерія/ФПВ) — всегда
    # типовое из класса, т.к. БД хранит смешанные пары и врёт (16 мин для балістики).
    weapon_has_eta = weapon in ("ballistic", "cruise_missile", "kab", "shahed", "fpv", "mlrs", "artillery")
    fast_weapons = ("ballistic", "kab", "mlrs", "artillery", "fpv")  # им нельзя доверять смешанный ETA
    if weapon_has_eta and stage in ("imminent", "unknown"):
        typical = weapon_eta(weapon)
        if weapon in fast_weapons:
            # Быстрое оружие — всегда типовое из класса (точнее смешанного ETA).
            lines.append(f"⏱ ETA: {typical}")
        elif regions:
            # Для крылатых/БПЛА — пробуем по региону.
            eta_db_type = "missile" if weapon == "cruise_missile" else "uav"
            est = estimate_eta(db, regions[0], weapon_type=eta_db_type)
            if est["available"] and est["avg_seconds"]:
                mins = int(est["avg_seconds"] / 60)
                lo = max(1, mins // 2)
                hi = mins * 2
                lines.append(
                    f"⏱ ETA: ~{mins} хв (за {est['samples']} істор. пар)  [~{lo} – ~{hi} хв]"
                )
            else:
                lines.append(f"⏱ Орієнтовний ETA: {typical}")
        else:
            lines.append(f"⏱ Орієнтовний ETA: {typical}")

    if weapon != "stand_down":
        icon = "🚨" if level == "CRITICAL" else "⚠️"
        lines.append(f"{icon} {recommendation}")

    lines.append("")
    lines.append(summary[:300])

    # Последствия из текста (если есть) — всегда показываем.
    casualties = _extract_casualties(text)
    if casualties:
        lines.append("")
        lines.append(casualties)

    if regions and weapon != "stand_down":
        region = regions[0]
        analysis = _build_analysis(db, region, weapon)
        if analysis:
            lines.append("")
            lines.append(analysis)

    # Тег источника внизу (без ID, только @username или название).
    if source and str(source) not in ("?", "None"):
        src = str(source)
        if src.lstrip("-").isdigit():
            lines.append(f"\n📡 Джерело: моніторинг")
        else:
            lines.append(f"\n📡 @{src}")

    return "\n".join(lines)[:4000]


def _detect_stage(text: str, threat_type: str) -> str:
    """Определить стадию угрозы по контексту.

    Возвращает 'past' (уже упал/взорвался), 'imminent' (пуски/летит/в направлении),
    'potential' (носители в море/возможен пуск) или 'unknown'.
    Нужно, чтобы не повышать критичность для предупредительных постов и
    не показывать ETA для уже свершившихся ударов.
    """
    lowered = text.lower()

    # Признаки СВЕРШИВШЕЙСЯ угрозы (прошлое время — прилёт уже произошёл).
    past_markers = (
        "упал", "упала", "упало", "упали", "упав",
        "впав", "впала", "впало",
        "врізався", "врезался",
        "збит", "збито", "сбит",
        "влучив", "влучила", "влучило",
        "вибухнув", "вибухнула",
        "вразив", "уражен", "поран", "загин",
        "руйнув", "пошкодж", "знищен",
    )

    # Признаки непосредственной угрозы.
    imminent_markers = (
        "пуск", "пуски", "лет", "курсом", "в направлении", "у напрямку",
        "рух", "йдé", "сброш", "зафіксован", "в повітр",
    )
    # ПРИОРИТЕТ: imminent проверяем ПЕРВЫМ. «БПЛА на Киев» — это imminent,
    # даже если где-то рядом есть слово «прильот» (оно может быть в другом контексте).
    if any(m in lowered for m in imminent_markers):
        return "imminent"

    # Проверяем past только если НЕТ imminent-маркеров.
    if any(m in lowered for m in past_markers):
        return "past"

    # Признаки потенциальной угрозы (без факта пуска).
    potential_markers = (
        "в море", "носител", "можлив", "очікуват", "загроза застосув",
        "загроза пуску", "підгот", "розгорнут", "маневр", "патрул",
        "акватор", "піднял", "зліт літ", "зліт міг", "зліт ту",
        "в повітрі немає", "станом на",
    )
    if any(m in lowered for m in potential_markers):
        return "potential"
    return "unknown"


def _severity_for_class(base_severity: str, stage: str) -> tuple[str, str, str]:
    """Критичность с учётом стадии. Потенциальная угроза → MODERATE."""
    if stage == "potential":
        return ("🟡", "MODERATE", "Попередження: можлива загроза. Бути напоготові.")
    if base_severity == "CRITICAL":
        return ("🔴", "CRITICAL", "НЕМЕДЛЕННО в укрытие!")
    if base_severity == "HIGH":
        return ("🟠", "HIGH", "Угроза. Немедленно в укрытие.")
    if base_severity == "ALL_CLEAR":
        return ("🟢", "ALL CLEAR", "Отбой. Можно выходить.")
    return ("🟡", "MODERATE", "Будьте напоготові.")


# Старые словари больше не нужны — берем из weapon_classes.
_TYPICAL_ETA = {}


def _typical_eta(threat_type: str) -> str:
    """Заглушка — используется weapon_eta() из weapon_classes."""
    return "~невідомо"


def _build_analysis(db: Database, region: str, threat_type: str) -> str:
    """Блок аналитики по региону: история прильотов, ПВО, риск-оценка."""
    lines: list[str] = ["📊 Аналіз:"]
    has_data = False

    counts = db.threat_counts(region=region, since=0)

    # История прильотов (explosion) в регионе.
    impacts = counts.get("explosion", 0)
    if impacts > 0:
        has_data = True
        # Частота: прильоты / всего угроз = доля «долетевших».
        total = sum(counts.values())
        if total > 0:
            pct = int(impacts * 100 / total)
            lines.append(f"💥 Прильоты в регіоні: {impacts} ({pct}% від усіх загроз).")

    # Кол-во угроз этого типа в регионе.
    type_count = counts.get(threat_type, 0)
    if type_count > 0:
        has_data = True
        lines.append(f"🎯 Загроз цього типу: {type_count} за весь архів.")

    # ETA по конкретному типу оружия в этом регионе (раздельно: ракеты/БПЛА).
    if threat_type in ("missile", "uav", "artillery"):
        est = estimate_eta(db, region, weapon_type=threat_type)
        if est["available"] and est["avg_seconds"]:
            mins = int(est["avg_seconds"] / 60)
            lines.append(f"⏱ Типовий час до удару: ~{mins} хв (за {est['samples']} пар).")

    # Типова тривалість тривоги.
    avg = db.avg_alert_duration(region)
    if avg is not None:
        has_data = True
        mins = int(avg / 60)
        h, m = mins // 60, mins % 60
        dur = f"~{h} год {m} хв" if h > 0 else f"~{mins} хв"
        lines.append(f"🕐 Типова тривалість тривоги: {dur}")

    # Риск-оценка на основе истории.
    risk = _risk_assessment(threat_type, type_count, impacts)
    if risk:
        has_data = True
        lines.append(f"🧠 Оцінка: {risk}")

    # Прогнозные блоки (forecast.py):
    # 1) Коридор укрытия — «сколько сидеть».
    sw = shelter_window(db, region)
    if sw:
        has_data = True
        lines.append(sw)

    # 2) Часовой риск — «сейчас активный час».
    hr = hourly_risk(db, region, threat_type)
    if hr:
        has_data = True
        lines.append(hr)

    # 3) Эскалация — если в воздухе авиация + множественные угрозы.
    active = db.active_threats(within_seconds=3600)
    esc = escalation_risk(db, region, active)
    if esc:
        has_data = True
        lines.append(esc)

    if not has_data:
        return ""
    return "\n".join(lines)


# Маркеры последствий для извлечения из текста (без LLM — быстро и точно).
_CASUALTY_MARKERS = {
    "поран": "🩸 Поранені",
    "ранен": "🩸 Поранені",
    "загин": "💀 Загиблі",
    "погиб": "💀 Загиблі",
    "загіб": "💀 Загиблі",
    "постраждав": "👥 Постраждалі",
    "пострада": "👥 Постраждалі",
    "пошкодж": "🏚 Пошкоджено",
    "руйнув": "🏚 Руйнування",
    "знищен": "🔥 Знищено",
    "горить": "🔥 Пожежа",
    "пожежа": "🔥 Пожежа",
    "горанн": "🔥 Пожежа",
}


def _extract_casualties(text: str) -> str:
    """Быстрое извлечение последствий из текста (без LLM).

    Возвращает строку с найденными маркерами (поранені/загиблі/пошкоджено)
    или пустую строку, если последствий нет.
    """
    lowered = text.lower()
    found: list[str] = []
    seen = set()
    for marker, label in _CASUALTY_MARKERS.items():
        if marker in lowered and label not in seen:
            found.append(label)
            seen.add(label)
    if not found:
        return ""
    return "💔 Наслідки: " + ", ".join(found)


def _risk_assessment(threat_type: str, type_count: int, impacts: int) -> str:
    """Краткая риск-оценка на основе архивных данных."""
    if threat_type == "missile":
        if impacts > 100:
            return "Високий ризик — регіон часто зазнає ракетних ударів. Укриття обов'язкове."
        if impacts > 0:
            return "Підвищений ризик — прильоти фіксувались. Перебувати у укритті."
        return "Загроза ракетного удару. Дотримуватись протоколу укриття."
    if threat_type == "uav":
        if impacts > 100:
            return "Високий ризик — БПЛА часто долітають. Укриття обов'язкове."
        if impacts > 0:
            return "Помірний ризик — фіксувались прильоти БПЛА. Укриття рекомендовано."
        return "Загроза БПЛА. Бути готовим до укриття."
    if threat_type == "artillery":
        return "Прямий ризик обстрілу. НЕМЕДЛЕННО у сховище."
    return ""
