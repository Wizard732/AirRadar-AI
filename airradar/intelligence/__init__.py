"""Домен «intelligence»: track_engine, incident_fusion, gazetteer, eta, forecast, wave_forecast, forecast_eval."""
from importlib import import_module as _im

MODULES = ('track_engine', 'incident_fusion', 'gazetteer', 'eta', 'forecast', 'wave_forecast', 'forecast_eval')


def __getattr__(name):
    if name in MODULES:
        import os
        real = os.path.join(os.path.dirname(__file__), name + ".py")
        # перенесені модулі живуть тут; решта поки що в корені (переекспорт)
        return _im(f"{__name__}.{name}") if os.path.exists(real) else _im(name)
    raise AttributeError(name)
