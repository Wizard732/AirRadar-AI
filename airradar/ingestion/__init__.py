"""Домен «ingestion»: source_policy, import_history, sync_history."""
from importlib import import_module as _im

MODULES = ('source_policy', 'import_history', 'sync_history')


def __getattr__(name):
    if name in MODULES:
        import os
        real = os.path.join(os.path.dirname(__file__), name + ".py")
        # перенесені модулі живуть тут; решта поки що в корені (переекспорт)
        return _im(f"{__name__}.{name}") if os.path.exists(real) else _im(name)
    raise AttributeError(name)
