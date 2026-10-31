"""Сумісний шим: реальний код живе в airradar/monitoring/moderation.py (старі імпорти працюють без змін)."""
import sys

from airradar.monitoring import moderation as _m

sys.modules[__name__] = _m
