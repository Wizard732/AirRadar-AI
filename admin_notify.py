"""Сумісний шим: реальний код живе в airradar/delivery/admin_notify.py (старі імпорти працюють без змін)."""
import sys

from airradar.delivery import admin_notify as _m

sys.modules[__name__] = _m
