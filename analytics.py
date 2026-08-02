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
    "missile": ("🔴", "CRITICAL", "Немедленно в укрытие! Ракетная угроза."),
    "uav": ("🟠", "HIGH", "Угроза БПЛА. Зайти в укрытие."),
    "artillery": ("🔴", "CRITICAL", "Обстрел! Немедленно в укрытие."),
    "explosion": ("🔴", "CRITICAL", "Взрывы! Оставаться в укрытии."),
    "stand_down": ("🟢", "ALL CLEAR", "Отбой. Можно выходить."),
    "other": ("🟡", "MODERATE", "Воздушная тревога. Будьте готовы."),
}

# Локализованные названия типов для заголовка.
TYPE_TITLE = {
    "missile": "Ракетна загроза",
    "uav": "БПЛА (Shahed/дрон)",
    "artillery": "Артилерійський обстріл",
    "explosion": "Прильот / вибухи",
    "stand_down": "Відбій тривоги",
    "other": "Повітряна тривога",
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

    # ETA — только для ракет/БПЛА/артиллерии (не для отбоя/тревоги/взрыва).
    if threat_type in ("missile", "uav", "artillery") and regions:
        est = estimate_eta(db, regions[0])
        if est["available"] and est["avg_seconds"]:
            mins = int(est["avg_seconds"] / 60)
            lo = max(1, mins // 2)
            hi = mins * 2
            lines.append(
                f"⏱ ETA: ~{mins} хв (за {est['samples']} істор. пар)  [~{lo} – ~{hi} хв]"
            )

    # Рекомендация (кроме отбоя).
    if threat_type != "stand_down":
        lines.append(f"⚠️ {recommendation}")

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


def _build_analysis(db: Database, region: str, threat_type: str) -> str:
    """Блок аналитики по региону: история прильотов, ПВО, длительность тревоги."""
    lines: list[str] = ["📊 Аналіз:"]
    has_data = False

    # История прильотов (explosion) в регионе.
    impacts = db.threat_counts(region=region, since=0).get("explosion", 0)
    if impacts > 0:
        has_data = True
        lines.append(f"💥 Тут раніше падало: {impacts} прильотів в архіві.")

    # Типова тривалість тривоги.
    avg = db.avg_alert_duration(region)
    if avg is not None:
        has_data = True
        mins = int(avg / 60)
        h, m = mins // 60, mins % 60
        dur = f"~{h} год {m} хв" if h > 0 else f"~{mins} хв"
        lines.append(f"🕐 Типова тривалість тривоги: {dur}")

    # Кол-во угроз этого типа в регионе.
    type_count = db.threat_counts(region=region, since=0).get(threat_type, 0)
    if type_count > 0:
        has_data = True
        lines.append(f"📈 Всього загроз цього типу в регіоні: {type_count}.")

    if not has_data:
        return ""
    lines.append(f"\n🔑 Джерела архіву: {region_name(region)}")
    return "\n".join(lines)
