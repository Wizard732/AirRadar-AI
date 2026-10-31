"""Сумісний шим: реальний код живе в airradar/processing/dedup.py (старі імпорти працюють без змін)."""
import sys

from airradar.processing import dedup as _m

sys.modules[__name__] = _m
