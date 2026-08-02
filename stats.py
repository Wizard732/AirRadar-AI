"""stats.py — детальная статистика (как в референс-боте).

Формирует расширенный отчёт за период (неделя/месяц):
  • За типами загроз
  • Основні місця прильотів (города)
  • Райони та об'єкти (цели)
  • Наслідки (последствия)
  • Тип зброї (у прильотах)
  • ППО / збиття
  • Повітряні тривоги (длительность)
"""

from __future__ import annotations

import time

from database import Database

# Локализованные подписи для типов угроз.
THREAT_LABELS = {
    "missile": "🚀 Ракетна загроза",
    "uav": "🛸 БПЛА (Shahed/дрон)",
    "artillery": "🔴 Артилерія",
    "explosion": "💥 Прильот / вибухи",
    "alert": "🟡 Повітряна тривога",
    "stand_down": "🟢 Відбій",
    "other": "🚨 Інше",
}


def format_detailed_stats(db: Database, days: int = 7, region: str | None = None) -> str:
    """Детальный отчёт за N дней (для всей Украины или одного региона)."""
    now = int(time.time())
    since = now - days * 86400

    lines: list[str] = []
    period = f"за {days} дн." if days < 30 else "за весь архів"
    reg = f" ({db.__class__})" if region else ""
    lines.append(f"📊 Статистика {period}\n")

    # 1) За типами загроз.
    counts = db.threat_counts(region=region, since=since)
    total = sum(counts.values())
    lines.append("🚨 За типами загроз:")
    if total:
        for t, c in sorted(counts.items(), key=lambda x: -x[1]):
            label = THREAT_LABELS.get(t, t)
            lines.append(f"  {label:30} {c}")
    else:
        lines.append("  (даних поки немає)")
    lines.append("")

    # 2) Основні місця прильотів (города из сущностей).
    cities = db.entity_counts("city", region=region, since=since)
    if cities:
        lines.append("📍 Основні місця прильотів:")
        for city, c in list(cities.items())[:8]:
            lines.append(f"  {city:30} {c}")
        lines.append("")

    # 3) Райони та об'єкти.
    targets = db.entity_counts("target", region=region, since=since)
    if targets:
        lines.append("🏗 Райони та об'єкти:")
        for tgt, c in list(targets.items())[:8]:
            lines.append(f"  {tgt:30} {c}")
        lines.append("")

    # 4) Наслідки.
    impacts = db.entity_counts("impact", region=region, since=since)
    if impacts:
        lines.append("💔 Наслідки:")
        for imp, c in list(impacts.items())[:8]:
            lines.append(f"  {imp:30} {c}")
        lines.append("")

    # 5) Тип зброї (у прильотах).
    weapons = db.entity_counts("weapon", region=region, since=since)
    if weapons:
        lines.append("🧨 Тип зброї (у прильотах):")
        for w, c in list(weapons.items())[:8]:
            lines.append(f"  {w:30} {c}")
        lines.append("")

    # 6) ППО / збиття.
    ppo = db.entity_counts("ppo", region=region, since=since)
    if ppo:
        lines.append("🛡 ППО / збиття:")
        for p, c in list(ppo.items())[:5]:
            lines.append(f"  {p:30} {c}")
        lines.append("")

    # 7) Повітряні тривоги (длительность).
    avg = db.avg_alert_duration(region) if region else None
    if avg is not None:
        mins = int(avg / 60)
        h, m = mins // 60, mins % 60
        lines.append("🚨 Повітряні тривоги:")
        lines.append(f"  Медіана тривалості: ~{h} год {m} хв" if h > 0 else f"  Медіана тривалості: ~{mins} хв")
        lines.append("")

    lines.append(f"🔄 Станом на {time.strftime('%H:%M:%S')}")
    return "\n".join(lines)[:3900]
