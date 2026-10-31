"""Сумісний шим: реальний код живе в airradar/safety/trust.py (старі імпорти працюють без змін)."""
import sys

from airradar.safety import trust as _m

sys.modules[__name__] = _m
