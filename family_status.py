"""Сумісний шим: реальний код живе в airradar/product/family_status.py (старі імпорти працюють без змін)."""
import sys

from airradar.product import family_status as _m

sys.modules[__name__] = _m
