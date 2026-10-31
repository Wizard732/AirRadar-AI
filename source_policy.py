"""Сумісний шим: реальний код живе в airradar/ingestion/source_policy.py (старі імпорти працюють без змін)."""
import sys

from airradar.ingestion import source_policy as _m

sys.modules[__name__] = _m
