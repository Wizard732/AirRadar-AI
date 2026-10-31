"""app_ref.py — доступ до модуля main без подвійного імпорту.

Сервіс запускається як `python main.py` (модуль `__main__`). Звичайний `import main` створив би ДРУГУ копію
з порожніми глобальними змінними (_publisher_ref тощо), тож спершу беремо вже запущений `__main__`.
"""
from __future__ import annotations

import sys


def main_module():
    m = sys.modules.get("__main__")
    if m is not None and hasattr(m, "_notify_subscribers"):
        return m
    import main
    return main
