"""Сумісний шим: реальний код живе в airradar/safety/event_lifecycle.py (старі імпорти працюють без змін)."""
import sys

from airradar.safety import event_lifecycle as _m

sys.modules[__name__] = _m
