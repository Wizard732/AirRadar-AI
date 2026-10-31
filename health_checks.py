"""Сумісний шим: реальний код живе в airradar/monitoring/health_checks.py (старі імпорти працюють без змін)."""
import sys

from airradar.monitoring import health_checks as _m

sys.modules[__name__] = _m
