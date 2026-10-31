"""Сумісний шим: реальний код живе в airradar/product/map_preview.py (старі імпорти працюють без змін)."""
import sys

from airradar.product import map_preview as _m

sys.modules[__name__] = _m
