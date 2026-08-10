"""Строгий отбор Telegram-постов для датасета оперативных сообщений."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fast_filter import KEYWORDS, clean_signature


@dataclass(frozen=True)
class FilterResult:
    """Итог предварительного отбора одного сообщения."""

    category: str  # active_threat | impact_or_shelling | irrelevant
    reason: str
    text: str


_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_REPEAT_RE = re.compile(r"(.)\1{5,}")

# Достаточно узкие признаки: сбор/реклама не являются оперативными сообщениями,
# даже если автор добавил в них слово «тривога» или «дрон».
_SPAM_MARKERS = (
    "збір", "сбор", "донат", "донатимо", "банка", "monobank", "моно банка",
    "поповн", "реклам", "розіграш", "розыгрыш", "промокод", "партнер",
    "ваканс", "робота в ", "работа в ", "купити", "купить", "продам",
    "підписуйтесь", "подписывайтесь", "підписатися", "подписаться",
)
_ANALYSIS_MARKERS = (
    "аналітика", "аналитика", "підсумок", "итоги", "огляд", "обзор",
    "прогноз", "тенденц", "статистик", "зведення за", "сводка за",
    "може бути", "может быть", "ймовірно", "вероятно", "очікується",
)
_ACTIVE_MARKERS = (
    "летить", "летит", "руха", "движ", "курсом", "напрямк", "у напрямку",
    "пряму", "наближа", "загроза", "тривога", "тревога", "пуск", "зліт",
    "в повітрі", "в воздухе", "у повітрі", "укритт", "укрыти",
)
_IMPACT_MARKERS = (
    "прильот", "прилет", "влучан", "попадан", "обстріл", "обстрел",
    "вибух", "взрыв", "детонац", "удар по", "атака на", "пожеж", "пожар",
    "загоріл", "загорел", "працює ппо", "работает пво",
)


def normalize_source_text(text: str) -> str:
    """Убрать подписи, ссылки и технический шум, сохранив факты поста."""
    text = clean_signature(text or "")
    text = _URL_RE.sub(" ", text)
    text = _REPEAT_RE.sub(r"\1\1\1", text)
    return _SPACE_RE.sub(" ", text).strip(" \t\r\n—–|•")


def classify_for_training(text: str) -> FilterResult:
    """Отнести пост к угрозе, факту удара либо нерелевантным данным.

    Это намеренно консервативный фильтр. Он лишь готовит кандидатов для LLM,
    окончательное включение в датасет определяется разметкой и валидатором.
    """
    cleaned = normalize_source_text(text)
    if len(cleaned) < 5:
        return FilterResult("irrelevant", "empty_or_too_short", cleaned)

    lowered = cleaned.lower()
    if any(marker in lowered for marker in _SPAM_MARKERS):
        return FilterResult("irrelevant", "promotion_or_fundraiser", cleaned)
    if any(marker in lowered for marker in _ANALYSIS_MARKERS):
        return FilterResult("irrelevant", "analysis_or_digest", cleaned)

    has_keyword = any(keyword in lowered for keyword in KEYWORDS)
    if any(marker in lowered for marker in _IMPACT_MARKERS) and has_keyword:
        return FilterResult("impact_or_shelling", "impact_marker", cleaned)
    if any(marker in lowered for marker in _ACTIVE_MARKERS) and has_keyword:
        return FilterResult("active_threat", "active_marker", cleaned)

    # Короткие посты мониторинговых каналов иногда не называют средство явно.
    has_motion = any(marker in lowered for marker in _ACTIVE_MARKERS)
    has_place_hint = any(word in lowered for word in ("на ", "до ", "над ", "по ", "у "))
    if has_motion and has_place_hint:
        return FilterResult("active_threat", "contextual_motion", cleaned)
    return FilterResult("irrelevant", "no_operational_signal", cleaned)
