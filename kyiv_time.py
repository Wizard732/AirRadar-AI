"""Сумісний шим: реальний код живе в airradar/ingestion/kyiv_time.py (старі імпорти працюють без змін)."""
import sys

from airradar.ingestion import kyiv_time as _m

sys.modules[__name__] = _m
