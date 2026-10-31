"""Сумісний шим: реальний код живе в airradar/safety/personal_status.py (старі імпорти працюють без змін)."""
import sys

from airradar.safety import personal_status as _m

sys.modules[__name__] = _m
