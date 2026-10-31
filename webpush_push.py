"""Сумісний шим: реальний код живе в airradar/delivery/webpush_push.py (старі імпорти працюють без змін)."""
import sys

from airradar.delivery import webpush_push as _m

sys.modules[__name__] = _m
