# -*- coding: utf-8 -*-
"""«Прогноз для моєї зони»: числа — детерміновані, нейромережа лише пояснює (без вигадок, з перевіркою виходу)."""
import asyncio

import ai_forecast
from database import Database
from track_engine import Risk


def _risk(level="imminent", approaching=True, pass_by=False, dist=18.0):
    return Risk(track_id="t", weapon="shahed", level=level, distance_km=dist, bearing_from_point=20.0, approaching=approaching,
                cpa_km=2.0, eta_min=6, speed_kmh=180.0, speed_source="measured", count=2, eta_range=(5, 8), pass_by=pass_by)


class FakeLLM:
    def __init__(self, out="Найближча ціль за 18 км, курс на вас — терміново."): self.out, self.seen = out, []
    async def classify(self, text, system_prompt, max_tokens=30):
        self.seen.append((text, system_prompt)); return self.out


def _zones(risks):
    return [{"label": "Дім", "radius": 10.0, "risks": [(r, ai_forecast.probability_label(r)) for r in risks]}]


def test_probability_labels_are_qualitative_and_ordered():
    L = ai_forecast.probability_label
    assert L(_risk("over")) == "дуже висока"
    assert L(_risk("imminent", approaching=True)) == "висока"
    assert L(_risk("imminent", approaching=True, pass_by=True)) == "низька–середня"
    assert L(_risk("imminent", approaching=False)) == "середня"
    assert L(_risk("watch")) == "низька"


def test_no_points_gives_setup_hint_without_llm(tmp_path):
    db = Database(str(tmp_path / "a.db")); llm = FakeLLM()
    t = asyncio.run(ai_forecast.forecast_text(db, llm, 1))
    assert "Мої місця" in t and llm.seen == []
    db.close()


def test_text_contains_computed_facts_and_ai_block(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "b.db")); llm = FakeLLM()
    monkeypatch.setattr(ai_forecast, "compute", lambda db_, uid, now=None: _zones([_risk()]))
    t = asyncio.run(ai_forecast.forecast_text(db, llm, 1))
    assert "Дім" in t and "висока" in t and "Пояснення ШІ" in t and "Орієнтовна оцінка" in t
    sent_facts, system = llm.seen[0]
    assert "18" in sent_facts and "Ймовірність підльоту: висока" in sent_facts
    assert "не вигадуй" in system.lower() and "координат" in system
    db.close()


def test_ai_failure_or_unsafe_output_falls_back_to_facts_only(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "c.db"))
    monkeypatch.setattr(ai_forecast, "compute", lambda db_, uid, now=None: _zones([_risk()]))

    class Boom:
        async def classify(self, *a, **k): raise RuntimeError("шлюз недоступний")
    assert "Пояснення ШІ" not in asyncio.run(ai_forecast.forecast_text(db, Boom(), 1))
    assert "Пояснення ШІ" not in asyncio.run(ai_forecast.forecast_text(db, FakeLLM(out=""), 1))
    leak = FakeLLM(out="ключ sk-" + "A1b2C3d4" * 6)
    t = asyncio.run(ai_forecast.forecast_text(db, leak, 1))
    assert "sk-A1b2" not in t and "Пояснення ШІ" not in t
    ok = asyncio.run(ai_forecast.forecast_text(db, None, 1))
    assert "висока" in ok and "Пояснення ШІ" not in ok
    db.close()


def test_html_is_escaped(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "d.db"))
    monkeypatch.setattr(ai_forecast, "compute", lambda db_, uid, now=None: [{"label": "<b>x</b>", "radius": 5.0, "risks": []}])
    t = asyncio.run(ai_forecast.forecast_text(db, FakeLLM(), 1))
    assert "&lt;b&gt;x&lt;/b&gt;" in t and "<b>x</b>" not in t
    db.close()


def test_thinking_budget_env(monkeypatch):
    import ai_summarizer as a
    monkeypatch.delenv("AI_THINKING_BUDGET", raising=False); monkeypatch.delenv("AI_DISABLE_THINKING", raising=False)
    assert a._thinking_budget() == 0 and a._thinking_extra() == {}
    monkeypatch.setenv("AI_THINKING_BUDGET", "500"); monkeypatch.setenv("AI_DISABLE_THINKING", "1")
    assert a._thinking_budget() == 500 and a._thinking_extra() == {"enable_thinking": False}
    monkeypatch.setenv("AI_THINKING_BUDGET", "ощось"); assert a._thinking_budget() == 0


def test_start_go_deeplinks_registered_and_work(tmp_path):
    """/start go_forecast і go_guard з Mini App обробляються гео-хендлерами; go_menu лишається для bot_ui."""
    import re
    from types import SimpleNamespace
    from telethon import events
    from test_bot_ui import FakeBot, _pattern_source
    from guard_handlers import register_guard_handlers
    db = Database(str(tmp_path / "e.db")); bot = FakeBot()
    register_guard_handlers(bot, db)

    def find(text):
        for args, kwargs, fn in bot.handlers:
            pat = _pattern_source(kwargs.get("pattern") or (getattr(args[0], "pattern", None) if args else None))
            m = re.match(pat, text) if pat else None
            if m and "go_" in pat:
                return fn, m
        return None, None

    async def run(text):
        fn, m = find(text)
        if fn is None:
            return None
        out = []
        async def respond(t, **kw): out.append(t)
        ev = SimpleNamespace(is_private=True, sender_id=5, pattern_match=m, respond=respond)
        try:
            await fn(ev)
        except events.StopPropagation:
            out.append("<stop>")
        return out
    fc = asyncio.run(run("/start go_forecast")); gd = asyncio.run(run("/start go_guard"))
    assert fc and "Прогноз для вашої зони" in fc[0] and fc[-1] == "<stop>"
    assert gd and "Сторож" in gd[0] and gd[-1] == "<stop>"
    assert asyncio.run(run("/start go_menu")) is None, "go_menu має дійти до bot_ui /start"
    db.close()


def test_more_deeplinks_respond(tmp_path):
    import re
    from types import SimpleNamespace
    from telethon import events
    from test_bot_ui import FakeBot, _pattern_source
    from guard_handlers import register_guard_handlers
    db = Database(str(tmp_path / "f.db")); bot = FakeBot(); register_guard_handlers(bot, db)

    async def run(text):
        for args, kwargs, fn in bot.handlers:
            pat = _pattern_source(kwargs.get("pattern") or (getattr(args[0], "pattern", None) if args else None))
            m = re.match(pat, text) if pat else None
            if m and "go_" in pat:
                out = []
                async def respond(t, **kw): out.append((t, kw.get("buttons")))
                try: await fn(SimpleNamespace(is_private=True, sender_id=5, pattern_match=m, respond=respond))
                except events.StopPropagation: pass
                return out
        return None
    for go in ("subs", "regions", "active", "report"):
        out = asyncio.run(run(f"/start go_{go}"))
        assert out and out[0][0], go
    assert asyncio.run(run("/start go_subs"))[0][1], "кнопки підписок"
    db.close()


def test_wave_command_and_deeplink(tmp_path):
    import re
    from types import SimpleNamespace
    from telethon import events
    from test_bot_ui import FakeBot, _pattern_source
    from guard_handlers import register_guard_handlers
    db = Database(str(tmp_path / "w.db")); bot = FakeBot(); register_guard_handlers(bot, db)

    async def run(text, uid=5):
        for args, kwargs, fn in bot.handlers:
            pat = _pattern_source(kwargs.get("pattern") or (getattr(args[0], "pattern", None) if args else None))
            m = re.match(pat, text) if pat else None
            if m and ("wave" in pat):
                out = []
                async def respond(t, **kw): out.append(t)
                try: await fn(SimpleNamespace(is_private=True, sender_id=uid, pattern_match=m, respond=respond))
                except events.StopPropagation: pass
                return out
        return None
    assert "Спершу оберіть область" in asyncio.run(run("/wave"))[0]
    db.subscribe(5, "kyivska")
    for cmd in ("/wave", "/start go_wave"):
        out = asyncio.run(run(cmd))
        assert out and "прогноз хвиль" in out[0] and "Київ" in out[0], cmd
    db.close()
