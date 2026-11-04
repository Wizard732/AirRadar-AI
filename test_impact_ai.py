# -*- coding: utf-8 -*-
"""Нейромережа розпізнає «вже наслідки» і витягує факти; вигадані числа/категорії відкидаються."""
import asyncio
import json

import impact_ai
import impact_log
from database import Database
from threat_parser import parse_post


class LLM:
    def __init__(self, out): self.out, self.calls = out, 0
    async def classify(self, text, prompt, max_tokens=30):
        self.calls += 1; return self.out if isinstance(self.out, str) else json.dumps(self.out, ensure_ascii=False)


def test_valid_hit_with_numbers_from_text():
    t = "У Харкові приліт по житловому будинку, 2 загинули, 5 поранені"
    r = impact_ai.parse_response(json.dumps({"is_impact": True, "kind": "hit", "killed": 2, "injured": 5, "children": None, "damage": ["житлові будинки"]}), t)
    assert r == {"kind": "hit", "killed": 2, "injured": 5, "children": None, "flags": "", "damage": ["житлові будинки"]}


def test_hallucinated_numbers_and_categories_are_dropped():
    t = "У Харкові приліт, є пошкодження"
    r = impact_ai.parse_response(json.dumps({"is_impact": True, "kind": "hit", "killed": 7, "injured": 3, "damage": ["житлові будинки", "космодром"]}), t)
    assert r["killed"] is None and r["injured"] is None and r["damage"] == ["житлові будинки"]


def test_threat_is_not_impact_and_bad_json_is_none():
    assert impact_ai.parse_response('{"is_impact": false, "kind": "none"}', "Шахеди курсом на Харків") is False
    assert impact_ai.parse_response("не json", "x") is None
    assert impact_ai.parse_response('{"is_impact": "так"}', "x") is None
    assert impact_ai.parse_response("", "x") is None


def test_classify_skips_llm_without_impact_words_and_survives_failure():
    llm = LLM({"is_impact": True, "kind": "hit"})
    assert asyncio.run(impact_ai.classify_impact(llm, "Шахеди курсом на Харків")) is None and llm.calls == 0

    class Boom:
        async def classify(self, *a, **k): raise RuntimeError("gw")
    assert asyncio.run(impact_ai.classify_impact(Boom(), "Приліт у Харкові, є загиблі")) is None
    assert asyncio.run(impact_ai.classify_impact(None, "Приліт у Харкові")) is None


def test_record_uses_ai_verdict(tmp_path):
    db = Database(str(tmp_path / "i.db"))
    text = "Харків: приліт по підприємству, 3 поранені"
    parsed = parse_post(text, "chan")
    # ШІ каже «не наслідки» (напр. «можливий приліт») — запису немає, хоча regex спрацював би
    assert impact_log.record_from_post(db, text, "chan", 1_700_000_000, parsed, info=False) is None
    ai = impact_ai.parse_response(json.dumps({"is_impact": True, "kind": "hit", "injured": 3, "damage": ["промисловість"]}), text)
    rec = impact_log.record_from_post(db, text, "chan", 1_700_000_000, parsed, info=ai)
    assert rec and rec["kind"] == "hit" and rec["injured"] == 3 and rec["damage"] == ["промисловість"]
    # без info працює старий regex-шлях
    rec2 = impact_log.record_from_post(db, "Харків: влучання по будинку, 1 загинув", "chan2", 1_700_100_000, parse_post("Харків: влучання по будинку, 1 загинув", "chan2"))
    assert rec2 and rec2["kind"] == "hit"
    db.close()
