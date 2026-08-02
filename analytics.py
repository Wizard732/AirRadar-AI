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
    threat_type = classify_threat(text)
    regions = detect_region(text)
    emoji, level, recommendation = SEVERITY.get(threat_type, SEVERITY["other"])
    title = TYPE_TITLE.get(threat_type, "Загроза")

    lines: list[str] = []
    # Заголовок с критичностью.
    lines.append(f"{emoji} {level}: {title}")

    # Регион/направление (берём первый определённый).
    if regions:
        lines.append(f"📍 Регіон: {region_name(regions[0])}")

    # ETA — для ракет/БПЛА/артиллерии (не для отбоя/тревоги/взрыва).
    # Сначала пробуем по региону; если данных мало — fallback на типовые ETA.
    if threat_type in ("missile", "uav", "artillery"):
        if regions:
            est = estimate_eta(db, regions[0])
            if est["available"] and est["avg_seconds"]:
                mins = int(est["avg_seconds"] / 60)
                lo = max(1, mins // 2)
                hi = mins * 2
                lines.append(
                    f"⏱ ETA: ~{mins} хв (за {est['samples']} істор. пар)  [~{lo} – ~{hi} хв]"
                )
            else:
                # Нет истории по региону — даём типовое время по типу оружия.
                typical = _typical_eta(threat_type)
                lines.append(f"⏱ Орієнтовний ETA: {typical}")
        else:
            # Регион не определён — даём типовое время по типу оружия.
            typical = _typical_eta(threat_type)
            lines.append(f"⏱ Орієнтовний ETA: {typical}")

    # Рекомендация (кроме отбоя) — с эмодзи по уровню критичности.
    if threat_type != "stand_down":
        icon = "🚨" if level in ("CRITICAL",) else "⚠️"
        lines.append(f"{icon} {recommendation}")

    # Сжатый текст источника.
    lines.append("")
    lines.append(f"💬 @{source}:" if not str(source).startswith("@") else f"💬 {source}:")
    lines.append(summary[:300])

    # Аналитика по региону — отдельным блоком (если есть данные).
    if regions and threat_type != "stand_down":
        region = regions[0]
        analysis = _build_analysis(db, region, threat_type)
        if analysis:
            lines.append("")
            lines.append(analysis)

    return "\n".join(lines)[:4000]


# Типовое время подлёта по типам оружия (если нет истории по региону).
# Основано на общих данных: КАБ долетает быстро, БПЛА — дольше.
_TYPICAL_ETA = {
    "missile": "~10–30 хв",
    "uav": "~20–60 хв",
    "artillery": "<5 хв",
}


def _typical_eta(threat_type: str) -> str:
    """Типовое ориентировочное время подлёта, если нет истории по региону."""
    return _TYPICAL_ETA.get(threat_type, "~невідомо")


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

    # ETA по конкретному типу оружия в этом регионе.
    if threat_type in ("missile", "uav", "artillery"):
        est = estimate_eta(db, region)
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

    if not has_data:
        return ""
    lines.append(f"\n🔑 Джерело: {region_name(region)}")
    return "\n".join(lines)


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
