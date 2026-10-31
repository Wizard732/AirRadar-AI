"""Сумісний шим: реальний код живе в airradar/safety/safety_policy.py (старі імпорти працюють без змін)."""
import sys

from airradar.safety import safety_policy as _m

sys.modules[__name__] = _m
