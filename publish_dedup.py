"""Сумісний шим: реальний код живе в airradar/processing/publish_dedup.py (старі імпорти працюють без змін)."""
import sys

from airradar.processing import publish_dedup as _m

sys.modules[__name__] = _m
