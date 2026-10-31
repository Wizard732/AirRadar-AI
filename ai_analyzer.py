"""Сумісний шим: реальний код живе в airradar/monitoring/ai_analyzer.py (старі імпорти працюють без змін)."""
import sys

from airradar.monitoring import ai_analyzer as _m

sys.modules[__name__] = _m
