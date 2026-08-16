"""Evidence-first public alerts: direct source facts, never forecasts."""

from __future__ import annotations

import time

from incident_fusion import IncidentFact
from regions import region_name
from weapon_classes import weapon_label


def _status(confirmation: dict) -> str:
    status = confirmation.get("status", "reported")
    if status == "officially_confirmed":
        return "повідомлено офіційним джерелом"
    if status == "corroborated":
        return f"підтверджено {confirmation.get('sources', 2)} незалежними джерелами"
    return "повідомлено одним джерелом; потребує незалежного підтвердження"


def render_evidence_alert(
    *, text: str, source: str, event_ts: int, fact: IncidentFact, confirmation: dict
) -> str:
    """Render only explicit source facts and clearly marked unknowns."""
    when = time.strftime("%H:%M", time.localtime(event_ts))
    title = weapon_label(fact.weapon_class)
    region = fact.destination_region
    count = "не зазначено"
    if confirmation.get("count_kind") == "reported_total":
        count = f"щонайменше {confirmation.get('count_value')} повідомлено"
    elif confirmation.get("count_kind") == "exact" and confirmation.get("count_value") is not None:
        count = str(confirmation["count_value"])
    elif confirmation.get("count_kind") == "conflicting":
        count = "у джерелах різниться; точне число невідоме"
    lines = [
        f"{title}",
        "",
        f"Статус: {_status(confirmation)}",
        f"Час повідомлення: {when}",
    ]
    if region and region != "unknown":
        lines.append(f"Регіон: {region_name(region)}")
    lines.extend(["", "Що повідомило джерело:", text[:700], "", "Відомо:"])
    lines.append(f"• Тип: {weapon_label(fact.weapon_class)}")
    if fact.destination_region and fact.destination_region != "unknown":
        lines.append(f"• Напрямок/регіон: {region_name(fact.destination_region)}")
    if fact.origin_region:
        lines.append(f"• Звідки: {region_name(fact.origin_region)}")
    lines.append(f"• Кількість: {count}")
    if fact.raw_designation:
        lines.append(f"• Назва з джерела: {fact.raw_designation}")
    unknown = ["точне місце", "подальший рух або наслідки"]
    if count == "не зазначено":
        unknown.insert(0, "точна кількість")
    lines.extend(["", "Не підтверджено або невідомо:"])
    lines.extend(f"• {item}" for item in unknown)
    lines.extend(["", f"Джерело: @{source}" if source and not source.lstrip("-").isdigit() else "Джерело: моніторинг"])
    return "\n".join(lines)[:4000]
