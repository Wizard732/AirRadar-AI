import pytest

import benchmark
import notification_rules as nr
import safety_policy as sp
import trust
from database import Database


def _prov(sources, observed=1000, received=1060, official=False, overrides=None):
    return trust.build_provenance(observed, received, sources, official=official, overrides=overrides)


def test_two_reposts_same_group_are_not_confirmed():
    p = _prov(["@a", "@b"], overrides={"a": "net", "b": "net"})
    assert p.source_count == 2 and p.source_independence == 1
    assert trust.classify(p, 1100) is trust.Confidence.PRELIMINARY


def test_two_independent_sources_confirmed_and_official_wins():
    assert trust.classify(_prov(["@a", "@b"]), 1100) is trust.Confidence.CONFIRMED
    assert trust.classify(_prov(["@a"], official=True), 1100) is trust.Confidence.OFFICIAL
    assert trust.classify(_prov([]), 1100) is trust.Confidence.UNCERTAIN


def test_stale_beats_confirmed():
    p = _prov(["@a", "@b"], official=True)
    assert trust.classify(p, 1000 + trust.DEFAULT_TTL_S + 1) is trust.Confidence.STALE


def test_ingest_lag_not_fresh():
    p = _prov(["@a"], observed=0, received=trust.STALE_LAG_S + 60)
    assert not trust.is_fresh(p, 700)
    assert "із затримкою" in trust.describe(p, 700)


def test_repost_clusters():
    c = trust.RepostClusters()
    c.add("@a", "Шахеди на Київ з боку Чернігова, курс на місто")
    c.add("@b", "шахеди на Київ з боку Чернігова курс на місто!")
    c.add("@c", "Вибухи у Харкові, ППО працює")
    assert c.independent_count() == 2


@pytest.mark.parametrize("text,violation", [
    ("Шахед, до вас 7 хв", "guaranteed_eta"),
    ("Шахед прилетить через 5 хвилин", "guaranteed_eta"),
    ("Ціль 50.4501, 30.5234", "exact_coordinates"),
    ("Ракета влучить у склад", "hit_forecast"),
    ("Офіційний відбій у Києві", "fake_official_status"),
])
def test_safety_blocks(text, violation):
    v = sp.check_publication(text)
    assert not v.allowed and violation in v.violations


def test_safety_allows_normal_and_strips_pct():
    assert sp.check_publication("Шахеди на Київщину").allowed
    v = sp.check_publication("Шахед на Суми, впевненість 97%")
    assert v.allowed and "97" not in v.text
    assert sp.check_publication("Офіційний відбій", from_official=True).allowed


def test_disclaimer_once():
    t = sp.with_disclaimer("x")
    assert sp.with_disclaimer(t) == t and "не замінює" in t


def test_critical_cannot_be_disabled():
    assert nr.user_wants(nr.Importance.CRITICAL, set())
    assert not nr.user_wants(nr.Importance.ANALYTIC, {nr.Importance.INFO})
    assert nr.importance_for(nr.Change.OFFICIAL_ALARM) is nr.Importance.CRITICAL
    assert nr.importance_for(nr.Change.NEW_EVENT, confirmed=True) is nr.Importance.CRITICAL
    assert nr.is_repeat((nr.Change.CLARIFIED, "x"), nr.Change.CLARIFIED, "x")


def test_delivery_only_forward():
    D = nr.Delivery
    assert nr.can_transition(D.SENT, D.DELIVERED)
    assert not nr.can_transition(D.OPENED, D.SENT)
    assert not nr.can_transition(D.ACKNOWLEDGED, D.FAILED)
    assert nr.can_transition(D.FAILED, D.QUEUED)


def test_db_delivery_and_error_reports(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert db.record_delivery("e1", "android", "critical", "created")
    assert db.record_delivery("e1", "android", "critical", "sent")
    assert not db.record_delivery("e1", "android", "critical", "queued")
    assert db.delivery_summary() == {"sent": 1}
    assert db.add_error_report(1, "k", "stale")
    assert not db.add_error_report(1, "k", "bogus")
    assert db.error_report_counts() == {"stale": 1}


import os  # noqa: E402


@pytest.mark.skipif(not os.path.exists("tests_data/ar_sample.jsonl.gz"), reason="набір постів не публікується")
def test_benchmark_smoke():
    rows = benchmark.load()[:300]
    res = benchmark.run(rows)
    assert res["posts"] == 300 and 0 <= res["false_critical_rate"] <= 1


import asyncio

import admin_notify
import ai_analyzer


def test_admin_recipients_super_plus_added(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    db.add_admin(777, 111111111)
    assert admin_notify.admin_recipients(db, 111111111) == [111111111, 777]
    assert admin_notify.admin_recipients(db, 0) == [777]


def test_notify_admins_only_admins_and_survives_failure(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    db.add_admin(777, 1)
    calls = []

    class Bot:
        async def send_message(self, uid, text, **kw):
            if uid == 1:
                raise RuntimeError("blocked")
            calls.append(uid)

    assert asyncio.run(admin_notify.notify_admins(Bot(), db, 1, "x")) == 1
    assert calls == [777]


def test_analyzer_finds_issues_and_uses_model(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    for _ in range(3):
        db.add_error_report(1, "k", "stale")

    class LLM:
        async def classify(self, text, system, max_tokens=0):
            return "Перевірити фільтри"

    out = asyncio.run(ai_analyzer.analyze(db, LLM()))
    assert "Звіти користувачів" in out and "Перевірити фільтри" in out
    clean = Database(str(tmp_path / "b.db"))
    assert "Проблем не знайдено" in asyncio.run(ai_analyzer.analyze(clean, LLM()))
