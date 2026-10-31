"""Домен «monitoring»: health_checks, ai_analyzer, benchmark, moderation."""
from importlib import import_module as _im

MODULES = ('health_checks', 'ai_analyzer', 'benchmark', 'moderation')


def __getattr__(name):
    if name in MODULES:
        import os
        real = os.path.join(os.path.dirname(__file__), name + ".py")
        # перенесені модулі живуть тут; решта поки що в корені (переекспорт)
        return _im(f"{__name__}.{name}") if os.path.exists(real) else _im(name)
    raise AttributeError(name)
