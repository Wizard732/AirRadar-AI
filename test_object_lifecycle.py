"""Тести життєвого циклу окремих об'єктів (airradar/safety/object_lifecycle.py) і кольорової семантики."""
import pytest

from airradar.safety import object_lifecycle as ol
from airradar.safety.object_lifecycle import Status as S, TransitionError

T0 = 1_700_000_000


def test_all_ten_states_exist():
    assert {s.value for s in S} == {"detected", "confirmed", "tracking", "stale", "lost", "intercepted",
                                    "destroyed", "fell", "completed", "expired"}


def test_new_object_is_detected_with_history():
    o = ol.create("a1", "shahed", T0, "src1")
    assert o.status is S.DETECTED and o.history[0].to == "detected"
    assert o.trust == "low"


def test_trust_grows_with_independent_sources():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 10, "s2")
    assert o.trust == "medium" and o.status is S.TRACKING
    ol.observe(o, T0 + 20, "s3")
    assert o.trust == "high"


def test_destroyed_requires_confirmation():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 5, "s2", confirmed=True)
    with pytest.raises(TransitionError):
        ol.close(o, S.DESTROYED, T0 + 60, source="")
    assert o.status is S.CONFIRMED  # без підтвердження статус не змінився
    ol.close(o, S.DESTROYED, T0 + 60, source="ППО-канал")
    assert o.status is S.DESTROYED and "ППО-канал" in o.confirmed_by
    assert o.closed_ts == T0 + 60 and not o.in_active_layer and not o.animated


@pytest.mark.parametrize("new", [S.INTERCEPTED, S.FELL])
def test_intercepted_and_fell_also_require_source(new):
    o = ol.create("a1", "shahed", T0, "s1")
    with pytest.raises(TransitionError):
        ol.close(o, new, T0 + 1, source="")


def test_silence_goes_stale_then_lost_never_destroyed():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 1, "s2")                       # tracking
    assert ol.evaluate_silence(o, T0 + 1 + ol.STALE_AFTER_S) is S.STALE
    assert not o.animated and o.in_active_layer        # stale: анімація стоп, але видно з попередженням
    assert ol.evaluate_silence(o, T0 + 1 + ol.LOST_AFTER_S) is S.LOST
    assert o.status is not S.DESTROYED and o.confirmed_by == set()


def test_resumed_data_returns_stale_to_tracking():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 1, "s2")
    ol.evaluate_silence(o, T0 + 1 + ol.STALE_AFTER_S)
    ol.observe(o, T0 + 1000, "s3")
    assert o.status is S.TRACKING and o.animated


def test_lost_leaves_active_layer_after_delay():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 1, "s2")
    ol.evaluate_silence(o, T0 + 1 + ol.LOST_AFTER_S)
    last = o.last_seen_ts
    assert ol.visible(o, last + ol.LOST_AFTER_S + 1)
    assert not ol.visible(o, last + ol.LOST_AFTER_S + ol.LOST_HIDE_AFTER_S + 1)


def test_closed_object_is_not_revived_by_repeat_reports():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.close(o, S.COMPLETED, T0 + 100, source="")
    ol.observe(o, T0 + 200, "s2")
    assert o.status is S.COMPLETED and o.last_seen_ts == T0


def test_expired_after_retention_and_terminal():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.close(o, S.DESTROYED, T0 + 10, source="канал")
    assert ol.evaluate_silence(o, T0 + 10 + ol.EXPIRE_AFTER_S) is S.EXPIRED
    with pytest.raises(TransitionError):
        ol.close(o, S.FELL, T0 + 20, source="канал")


def test_invalid_transition_rejected():
    o = ol.create("a1", "shahed", T0, "s1")
    with pytest.raises(TransitionError):
        ol.close(o, S.EXPIRED, T0, source="x")   # expired напряму недозволено


def test_history_records_every_change():
    o = ol.create("a1", "shahed", T0, "s1")
    ol.observe(o, T0 + 1, "s2")
    ol.evaluate_silence(o, T0 + 1 + ol.STALE_AFTER_S)
    ol.close(o, S.DESTROYED, T0 + 2000, source="канал")
    assert [c.to for c in o.history] == ["detected", "tracking", "stale", "destroyed"]


@pytest.mark.parametrize("kind,color", [
    ("shahed", "#ffd60a"), ("uav", "#ffd60a"), ("fpv", "#ffd60a"),
    ("cruise_missile", "#ff3b30"), ("ballistic", "#ff3b30"), ("missile", "#ff3b30"),
    ("kab", "#ffb020"), ("aviation", "#5ac8fa"), ("mlrs", "#ff9f0a"),
])
def test_color_follows_normalized_kind(kind, color):
    assert ol.color_for(kind) == color


def test_unknown_kind_is_neutral_not_drone_or_missile():
    assert ol.class_of("something_new") == "other"
    assert ol.color_for("") == ol.CLASS_COLOR["other"]
