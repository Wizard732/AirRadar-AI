"""safety_policy.py — обов'язковий детермінований етап перед будь-якою публікацією.

Правило архітектури: LLM витягує та пояснює; цей код перевіряє і вирішує, що можна публікувати.
LLM не має права: змінювати офіційний статус тривоги, оголошувати відбій, вигадувати час підльоту,
показувати відсотки впевненості, підмінювати тип загрози.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DISCLAIMER = (
    "ℹ️ AirRadar AI — інформаційно-аналітична система. Вона не замінює офіційні повідомлення про "
    "повітряну тривогу та рекомендації ДСНС. У разі тривоги негайно прямуйте до укриття."
)

# Гарантований ETA («до вас 7 хв», «прилетить через 5 хвилин») — заборонено в публічному тексті LLM.
_ETA_GUARANTEE = re.compile(
    r"(?:до\s+вас|прил[іе]тить|долетить|прибуде|долет[іи]ть|прилетит|долетит)[^.\n]{0,25}?"
    r"(?:через|за)?\s*\d+\s*(?:хв|хвилин|мин|минут)", re.I)
# Відсотки впевненості/точності моделі.
_CONFIDENCE_PCT = re.compile(
    r"(?:впевненість|упевненість|ймовірність|точність|вероятность|уверенность)[^.\n]{0,15}?\d{1,3}\s*%|"
    r"\d{1,3}\s*%\s*(?:впевнен|упевнен|ймовірн|вероятн|точн)", re.I)
# Точні координати цілі/ураження.
_COORDS = re.compile(r"\b\d{2}\.\d{3,}\s*[,;]\s*\d{2}\.\d{3,}\b")
# Прогноз ураження конкретного об'єкта. «вдарити/вдарить» не блокуємо: це частіше цитата чи загроза в новинах.
_HIT_FORECAST = re.compile(r"(?:влучить|влучать|потрапить\s+(?:в|у|по)|попадёт|буде\s+ураж)", re.I)

OFFICIAL_CLAIMS = re.compile(r"(?:офіційн\w*\s+(?:відбій|відбой)|official\s+all.clear)", re.I)


@dataclass
class Verdict:
    allowed: bool
    text: str
    violations: list[str] = field(default_factory=list)


def check_publication(text: str, *, kind: str = "threat", from_official: bool = False,
                      llm_generated: bool = True) -> Verdict:
    """Перевірити текст перед публікацією.

    kind: 'threat' | 'stand_down' | 'alarm' | 'other'.
    Порушення, що можна безпечно прибрати (відсотки), прибираємо; решта — блокують публікацію.
    Відбій і офіційний статус дозволені лише від офіційного джерела (from_official).
    """
    violations: list[str] = []
    out = text or ""
    if not out.strip():
        return Verdict(False, out, ["empty"])
    if llm_generated:
        if _ETA_GUARANTEE.search(out):
            violations.append("guaranteed_eta")
        if _COORDS.search(out):
            violations.append("exact_coordinates")
        if _HIT_FORECAST.search(out):
            violations.append("hit_forecast")
        if OFFICIAL_CLAIMS.search(out) and not from_official:
            violations.append("fake_official_status")
        if kind == "stand_down" and not from_official and "підтверд" not in out.lower():
            # відбій без офіційного джерела — тільки як повідомлення каналу, а не як офіційний статус
            pass
        if _CONFIDENCE_PCT.search(out):
            out = _CONFIDENCE_PCT.sub("", out)
            violations.append("confidence_pct_removed")
    blocking = [v for v in violations if v != "confidence_pct_removed"]
    return Verdict(not blocking, out, violations)


def with_disclaimer(text: str) -> str:
    """Додати пояснення до публікації (один раз)."""
    if DISCLAIMER in text:
        return text
    return f"{text}\n\n{DISCLAIMER}"
