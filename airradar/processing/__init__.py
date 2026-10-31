"""Домен «processing»: threat_parser, dedup, media_ocr, fast_filter, publish_dedup."""
from importlib import import_module as _im

MODULES = ('threat_parser', 'dedup', 'media_ocr', 'fast_filter', 'publish_dedup')


def __getattr__(name):
    if name in MODULES:
        import os
        real = os.path.join(os.path.dirname(__file__), name + ".py")
        # перенесені модулі живуть тут; решта поки що в корені (переекспорт)
        return _im(f"{__name__}.{name}") if os.path.exists(real) else _im(name)
    raise AttributeError(name)
