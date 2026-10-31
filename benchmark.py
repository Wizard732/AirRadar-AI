"""Сумісний шим: реальний код живе в airradar/monitoring/benchmark.py (старі імпорти працюють без змін)."""
import sys

from airradar.monitoring import benchmark as _m

sys.modules[__name__] = _m

if __name__ == "__main__":
    _m.main()
