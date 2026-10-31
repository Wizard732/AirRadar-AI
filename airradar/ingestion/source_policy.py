"""Нормализация источников и политика независимого подтверждения."""

from __future__ import annotations


def normalize_source(source: str) -> str:
    """Единый ключ для username и числового Telegram ID."""
    value = str(source or "").strip().lower()
    if value.startswith("@"):
        value = value[1:]
    return value


def source_group(source: str, overrides: dict[str, str] | None = None) -> str:
    """Вернуть группу независимости; репосты одной группы не подтверждают друг друга."""
    key = normalize_source(source)
    return normalize_source((overrides or {}).get(key, key))


def parse_source_groups(raw: str) -> dict[str, str]:
    """Разобрать SOURCE_GROUPS: ``@a=network_a,@b=network_a``."""
    groups: dict[str, str] = {}
    for pair in raw.split(","):
        source, separator, group = pair.partition("=")
        if separator and source.strip() and group.strip():
            groups[normalize_source(source)] = normalize_source(group)
    return groups
