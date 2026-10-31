"""Сумісний шим: реальний код живе в airradar/safety/notification_rules.py (старі імпорти працюють без змін)."""
import sys

from airradar.safety import notification_rules as _m

sys.modules[__name__] = _m
