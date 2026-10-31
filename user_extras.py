"""Сумісний шим: реальний код живе в airradar/product/user_extras.py (старі імпорти працюють без змін)."""
import sys

from airradar.product import user_extras as _m

sys.modules[__name__] = _m
