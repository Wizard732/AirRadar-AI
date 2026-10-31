import asyncio
import collections

import event_lifecycle as lc
import family
import family_status as fst
import health_checks as hc
import moderation
import personal_status
import user_extras
from database import Database


def _db(tmp_path, name="p.db"):
    return Database(str(tmp_path / name))


def test_lifecycle_forward_only(tmp_path):
    db = _db(tmp_path)
    assert lc.advance(db, "e", lc.Stage.CREATED)
    assert lc.advance(db, "e", lc.Stage.VERIFIED)
    assert not lc.advance(db, "e", lc.Stage.OBSERVED)
    assert lc.advance(db, "e", lc.Stage.UPDATED) and lc.advance(db, "e", lc.Stage.UPDATED)
    assert lc.is_active(db, "e")
    assert lc.resolve_active(db) == 1 and not lc.is_active(db, "e")
    assert not lc.advance(db, "e", lc.Stage.UPDATED)
    assert lc.archive_old(db, now=10**10) == 1
    assert lc.stage_of(db, "e") is lc.Stage.ARCHIVED and not lc.advance(db, "e", lc.Stage.RESOLVED)


def test_family_unknown_is_not_danger(tmp_path):
    db = _db(tmp_path)
    code = family.create_invite(db, 1)
    ok, _, _ = family.accept_invite(db, code, 2)
    assert ok
    txt = fst.board_text(db, 1, now=1000)
    assert "невідомий" in txt and "не означає небезпеку" in txt and "🆘" not in txt
    assert fst.set_status(db, 2, "help", now=1000)
    assert "Потрібна допомога" in fst.board_text(db, 1, now=1100)
    assert fst.get_status(db, 2, now=1000 + fst.TTL_S + 1)[0] == "unknown"
    assert not fst.set_status(db, 2, "bogus")


def test_family_alarm_prompt_cooldown(tmp_path):
    db = _db(tmp_path)
    code = family.create_invite(db, 1)
    family.accept_invite(db, code, 2)
    db.subscribe(1, "kyivska")
    sent = []

    class Bot:
        async def send_message(self, uid, text, **kw):
            sent.append(uid)

    assert asyncio.run(fst.send_alarm_prompt(db, Bot(), ["kyivska"], now=1000)) == 1
    assert asyncio.run(fst.send_alarm_prompt(db, Bot(), ["kyivska"], now=1100)) == 0
    assert asyncio.run(fst.send_alarm_prompt(db, Bot(), ["kyivska"], now=1000 + fst.PROMPT_COOLDOWN_S)) == 1


def test_diagnose_source_distinguishes_cases():
    now = 10**6
    C = collections.Counter
    assert hc.diagnose_source({"disabled": 1}, None, now)[0] == "disabled"
    assert hc.diagnose_source({"last_seen": now - 10}, C(received=5, filtered=5), now)[0] == "all_filtered"
    assert hc.diagnose_source({"last_seen": now - 5 * 3600, "error_count": 0}, None, now)[0] == "silent"
    assert hc.diagnose_source({"last_seen": now - 5 * 3600, "error_count": 9}, None, now)[0] == "unavailable"
    assert hc.diagnose_source({"last_seen": now - 60, "error_count": 0}, C(received=3, processed=1), now)[0] == "ok"


def test_synthetic_check_is_isolated():
    r = hc.synthetic_check()
    assert r["safety_policy"] and r["isolated"]


def test_moderation_flow(tmp_path):
    db = _db(tmp_path)
    db.add_error_report(5, "k1", "wrong_city")
    db.add_error_report(6, "k2", "stale")
    assert len(moderation.open_reports(db)) == 2
    assert moderation.resolve(db, 1, "confirmed") and not moderation.resolve(db, 1, "dismissed")
    assert moderation.resolve(db, 2, "dismissed") and not moderation.resolve(db, 2, "bogus")
    assert '"wrong_city"' in moderation.export_confirmed(db) and "k2" not in moderation.export_confirmed(db)
    assert "порожня" in moderation.queue_text(db)


def test_personal_status_separates_official_and_analytics(tmp_path):
    db = _db(tmp_path)
    assert "Підпишись" in personal_status.render(db, 9) or "підпишись" in personal_status.render(db, 9)
    db.subscribe(9, "kyivska")
    t = personal_status.render(db, 9, now=1000)
    assert "Офіційна тривога" in t and "дані сирен недоступні" in t and "не замінює" in t
    db.set_siren_state("kyivska", True)
    import time
    t = personal_status.render(db, 9, now=int(time.time()))
    assert "АКТИВНА" in t and "не офіційно" in t


def test_checklist_and_insights(tmp_path):
    db = _db(tmp_path)
    assert user_extras.toggle(db, 1, "water") and user_extras.done(db, 1) == {"water"}
    assert user_extras.toggle(db, 1, "water") and not user_extras.done(db, 1)
    assert not user_extras.toggle(db, 1, "nope")
    assert "Спочатку" in user_extras.insights_text(db, 1)


# ---- інтеграція з ботом (FakeBot із test_bot_ui) ----
from test_bot_ui import ADMIN_ID, FakeBot, FakeCallbackEvent, FakeMessageEvent  # noqa: E402
from bot_ui import register_handlers  # noqa: E402
from admin_ui import register_admin_handlers  # noqa: E402
from user_commands import register_user_commands  # noqa: E402


def _cb(bot, uid, data):
    ev = FakeCallbackEvent(data.encode(), uid)
    asyncio.run(bot.dispatch_callback(ev))
    return ev


def test_menu_callbacks_new_screens(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    register_handlers(bot, db, ADMIN_ID, "", "https://x.github.io/AirRadar-AI/")
    for data in ("mys", "chk", "chk:water", "ins", "off", "fam"):
        ev = _cb(bot, 77, data)
        assert ev.edits, data
    assert "Чек-лист" in _cb(bot, 77, "chk").edits[0]["text"]
    ev = _cb(bot, 77, "fam:st:help")
    assert fst.get_status(db, 77)[0] == "help"


def test_error_report_flow_goes_to_admins_only(tmp_path):
    db = _db(tmp_path)
    db.add_admin(555, ADMIN_ID)
    sent = []

    class B(FakeBot):
        async def send_message(self, uid, text, **kw):
            sent.append(uid)

    bot = B()
    register_handlers(bot, db, ADMIN_ID, "", "")
    ev = _cb(bot, 42, "fbe:abc")
    assert any("Що не так" in r for r in ev.responses)
    _cb(bot, 42, "fbc:stale:abc")
    assert db.error_report_counts() == {"stale": 1}
    assert set(sent) == {ADMIN_ID, 555}


def test_user_commands_and_admin_commands(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    register_user_commands(bot, db, "", lambda uid: uid == ADMIN_ID)
    register_admin_handlers(bot, db, ADMIN_ID)
    names = {fn.__name__ for _, _, fn in bot.handlers}
    assert {"_my", "_help", "_checklist", "_reports", "_health", "_selftest", "_analyze"} <= names
    fn = next(f for _, _, f in bot.handlers if f.__name__ == "_help")
    ev = FakeMessageEvent("/help", 5)
    asyncio.run(fn(ev))
    assert "Команди" in ev.responses[0]
    # не-адмін не бачить службових команд
    rep = next(f for _, _, f in bot.handlers if f.__name__ == "_reports")
    ev = FakeMessageEvent("/reports", 5)
    asyncio.run(rep(ev))
    assert ev.responses == []
    ev = FakeMessageEvent("/reports", ADMIN_ID)
    asyncio.run(rep(ev))
    assert ev.responses


def test_map_preview_png_and_privacy():
    import map_preview
    png = map_preview.render_png({"kyivska"})
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png.endswith(b"IEND\xaeB`\x82") and len(png) > 500
    cap = map_preview.caption({"kyivska"}, "10:00")
    assert "Офіційна тривога" in cap and "не замінює" in cap
    assert "Офіційних тривог" in map_preview.caption(set(), "10:00")


def test_notify_levels_critical_never_off(tmp_path):
    db = _db(tmp_path)
    assert db.toggle_notify_level(1, "analytic") and db.get_notify_levels(1) == {"analytic"}
    assert db.toggle_notify_level(1, "analytic") and db.get_notify_levels(1) == set()
    assert not db.toggle_notify_level(1, "critical")


def test_notify_subscribers_respects_levels(tmp_path):
    import main as m
    db = _db(tmp_path)
    db.subscribe(1, "kyivska"); db.subscribe(2, "kyivska")
    db.toggle_notify_level(1, "analytic")
    sent = []

    class B:
        async def send_message(self, uid, text, **kw):
            sent.append(uid)

    asyncio.run(m._notify_subscribers(B(), db, ["kyivska"], "прогноз", importance="analytic"))
    assert sent == [2]
    sent.clear()
    db.toggle_notify_level(2, "high")
    asyncio.run(m._notify_subscribers(B(), db, ["kyivska"], "🚨 ПОВІТРЯНА ТРИВОГА\nКиїв"))
    assert sorted(sent) == [1, 2]


def test_stale_lag_line_in_alert():
    from alert_renderer import render_evidence_alert
    from incident_fusion import extract_incident_fact
    t = "Шахеди на Київщину"
    f = extract_incident_fact(t, "shahed", "imminent")
    old = render_evidence_alert(text=t, source="a", event_ts=1000, fact=f, confirmation={}, lag_s=1200)
    new = render_evidence_alert(text=t, source="a", event_ts=1000, fact=f, confirmation={}, lag_s=30)
    assert "застарілою" in old and "застарілою" not in new


def test_checklist_reminder_schedule(tmp_path):
    from kyiv_time import kyiv_midnight_ts
    db = _db(tmp_path)
    assert user_extras.toggle_remind(db, 1) is True
    base = kyiv_midnight_ts(1_800_000_000)
    assert user_extras.due_reminders(db, base + 10 * 3600) == []          # не та година
    assert user_extras.due_reminders(db, base + 19 * 3600 + 60) == [1]
    assert user_extras.due_reminders(db, base + 19 * 3600 + 120) == []    # раз на добу
    assert user_extras.toggle_remind(db, 1) is False


def test_ack_button_and_delivery_ack(tmp_path):
    import main as m
    db = _db(tmp_path)
    db.subscribe(1, "kyivska")
    seen = []

    class B:
        async def send_message(self, uid, text, **kw):
            seen.append(kw.get("buttons"))

    asyncio.run(m._notify_subscribers(B(), db, ["kyivska"], "🚨 ПОВІТРЯНА ТРИВОГА\nКиїв"))
    from test_bot_ui import _button_datas
    datas = _button_datas(seen[0])
    assert any(d.startswith("ack:") for d in datas)
    assert db.delivery_summary() == {"sent": 1}
    key = next(d for d in datas if d.startswith("ack:"))[4:]
    bot = FakeBot()
    register_handlers(bot, db, ADMIN_ID, "", "")
    _cb(bot, 1, "ack:" + key)
    assert db.delivery_summary() == {"acknowledged": 1}


def test_ntfy_ack_action_and_start_ack(tmp_path):
    import push_fcm
    p = push_fcm.build_ntfy("ar-x", "t", "b", "alarm", ack="abc123")
    assert p["actions"][0]["label"] == "Отримав" and p["actions"][0]["url"].endswith("?start=ack_abc123")
    assert "actions" not in push_fcm.build_ntfy("ar-x", "t", "b", "info")
    from guard_handlers import register_guard_handlers
    db = _db(tmp_path)
    db.record_delivery("abc123", "ntfy:7", "critical", "sent")
    bot = FakeBot()
    register_guard_handlers(bot, db)
    fn = next(f for _, _, f in bot.handlers if f.__name__ == "_ack_from_push")
    ev = FakeMessageEvent("/start ack_abc123", 7)
    import re
    ev.pattern_match = re.match(r"^/start(?:@\w+)?\s+ack_(\w+)\s*$", ev.text)
    try:
        asyncio.run(fn(ev))
    except BaseException:  # StopPropagation
        pass
    assert db.delivery_summary() == {"acknowledged": 1}


def test_delivery_probe_only_to_requesting_admin(tmp_path):
    db = _db(tmp_path)
    sent = []

    class B:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))

    r = asyncio.run(hc.delivery_probe(db, B(), 42, now=100))
    assert r["telegram"] is True and sent == [(42, hc.SYNTHETIC_TEXT)]
    assert db.delivery_summary() == {"sent": 1}


def test_independence_eval_smoke():  # не потребує набору даних
    import eval_independence as ei
    rows = [{"event_ts": 1, "source": "a", "text": "Шахеди на Київ з Чернігова курс на місто"},
            {"event_ts": 5, "source": "b", "text": "шахеди на Київ з Чернігова курс на місто"},
            {"event_ts": 9, "source": "c", "text": "Вибухи у Харкові працює ППО"}]
    r = ei.run(rows, 600)
    assert r["posts"] == 3 and r["cluster_size_histogram"] == {1: 1, 2: 1}


def test_secrets_check_detects_and_hides(tmp_path):
    import secrets_check as sc
    (tmp_path / "a.py").write_text("TOKEN = 'gsk_" + "A" * 30 + "'\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    found = sc.scan(tmp_path)
    assert ("a.py", "groq_key") in found and not any(f == "ok.py" for f, _ in found)


def test_airradar_domain_package_maps_real_modules():
    import importlib
    import airradar
    for dom in airradar.DOMAINS:
        pkg = importlib.import_module(f"airradar.{dom}")
        for name in pkg.MODULES:
            assert getattr(pkg, name).__name__.endswith(name)
    from airradar.database import Database as D
    assert D is Database


def test_webpush_pairing_and_gating(tmp_path, monkeypatch):
    import base64, json
    import webpush_push as wp
    db = _db(tmp_path)
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "k", "auth": "a"}}
    code = base64.urlsafe_b64encode(json.dumps(sub).encode()).rstrip(b"=").decode()
    assert wp.decode_code(code) == sub and wp.decode_code("zzz") is None
    ok, _ = wp.pair(db, 5, code)
    assert ok and wp.subs_of(db, 5) == [sub]
    monkeypatch.setenv("VAPID_PRIVATE_KEY_FILE", str(tmp_path / "none.pem"))
    assert not wp.configured() and asyncio.run(wp.push_user(db, 5, "t", "b")) == 0
    assert wp.unpair_all(db, 5) == 1
    for i in range(wp.MAX_DEVICES + 1):
        s = dict(sub, endpoint=f"https://x/{i}")
        r = wp.pair(db, 6, base64.urlsafe_b64encode(json.dumps(s).encode()).decode())
    assert r[0] is False


def test_webpush_send_marks_gone_subscriptions(tmp_path, monkeypatch):
    import base64, json
    import webpush_push as wp
    db = _db(tmp_path)
    pem = tmp_path / "v.pem"; pem.write_text("x")
    monkeypatch.setenv("VAPID_PRIVATE_KEY_FILE", str(pem))
    sub = {"endpoint": "https://web.push.apple.com/gone", "keys": {"p256dh": "k", "auth": "a"}}
    wp.pair(db, 5, base64.urlsafe_b64encode(json.dumps(sub).encode()).decode())
    monkeypatch.setattr(wp, "_send_sync", lambda s, p: 410)
    assert asyncio.run(wp.push_user(db, 5, "t", "b")) == 0 and wp.subs_of(db, 5) == []


def test_calendar_and_safety_narrowing(tmp_path):
    import safety_policy as sp
    db = _db(tmp_path)
    assert "Спочатку" in user_extras.calendar_text(db, 1)
    db.subscribe(1, "kyivska")
    assert "Календар тривог" in user_extras.calendar_text(db, 1)
    assert sp.check_publication("Ми можемо вдарити по Україні ракетою").allowed
    assert not sp.check_publication("Ракета влучить у склад").allowed


def test_alarm_preview_cooldown_and_photo(tmp_path, monkeypatch):
    import alarm_notices as an
    db = _db(tmp_path)
    db.set_siren_state("kyivska", True)
    photos = []

    class P:
        async def send_photo(self, png, caption):
            photos.append((png[:4], caption))
            return True

    asyncio.run(an._maybe_preview(db, P(), 1000))
    asyncio.run(an._maybe_preview(db, P(), 1100))     # в межах 10 хв — ні
    asyncio.run(an._maybe_preview(db, P(), 1000 + an.PREVIEW_COOLDOWN_S))
    assert len(photos) == 2 and photos[0][0] == b"\x89PNG" and "Офіційна тривога" in photos[0][1]
    monkeypatch.setenv("ALARM_MAP_PREVIEW", "0")
    asyncio.run(an._maybe_preview(db, P(), 10**6))
    assert len(photos) == 2


def test_analyzer_llm_selection(monkeypatch):
    import ai_analyzer
    monkeypatch.delenv("ANALYZER_API_KEY", raising=False)
    sentinel = object()
    assert ai_analyzer.make_llm(sentinel) is sentinel
    monkeypatch.setenv("ANALYZER_API_KEY", "k")
    llm = ai_analyzer.make_llm(sentinel)
    assert llm is not sentinel and llm._url == "https://ru.cheapvibecode.ru/v1/chat/completions"
    assert llm._model == "claude-sonnet-5"


def test_overlapping_alarms_not_double_counted(tmp_path):
    import user_extras as ux
    assert ux._merge_intervals([(0, 100), (50, 150), (200, 300)], 1000) == [(0, 150), (200, 300)]
    assert ux._merge_intervals([(10, None)], 100) == [(10, 100)]
    db = _db(tmp_path)
    db.subscribe(1, "kyiv_left"); db.subscribe(1, "kyiv_right")
    now = 1_800_000_000
    with db._lock:
        for slug in ("kyiv_left", "kyiv_right"):
            db._conn.execute("INSERT INTO alert_log (slug, loc_type, title, raion, level, kind, started_ts, ended_ts) VALUES (?,?,?,?,?,?,?,?)",
                             (slug, "oblast", slug, "", 0, "air_raid", now - 7200, now - 3600))
        db._conn.commit()
    t = ux.insights_text(db, 1, now=now)
    assert "<b>1</b>" in t and "1 год 0 хв" in t


def test_moderation_fix_and_stale(tmp_path):
    import event_lifecycle as lcm
    db = _db(tmp_path)
    with db._lock:
        db._conn.execute("INSERT INTO incidents (incident_key, created_ts, updated_ts, weapon_class, stage, region, source_count, sources, status, origin_region, destination_region, count_kind, count_value, weapon_raw, state) VALUES ('k1',1,1,'uav','imminent','kyivska',1,'a','reported','','','', 0,'','open')")
        db._conn.commit()
    assert moderation.apply_fix(db, 9, "k1", "region", "lvivska")[0]
    assert not moderation.apply_fix(db, 9, "k1", "region", "nope")[0]
    assert not moderation.apply_fix(db, 9, "k1", "type", "bogus")[0]
    assert not moderation.apply_fix(db, 9, "zzz", "region", "lvivska")[0]
    assert moderation.apply_fix(db, 9, "k1", "type", "shahed")[0]
    with db._lock:
        assert db._conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 2
    lcm.advance(db, "k1", lcm.Stage.CREATED)
    assert moderation.mark_stale(db, "k1") and not lcm.is_active(db, "k1")


def test_voice_text_is_deterministic_and_official(tmp_path):
    import time
    from airradar.product import voice
    db = _db(tmp_path)
    assert "підпишіться" in voice.voice_text(db, 1)
    db.subscribe(1, "kyivska")
    assert "недоступні" in voice.voice_text(db, 1, now=1000)
    db.set_siren_state("kyivska", True)
    t = voice.voice_text(db, 1, now=int(time.time()))
    assert "триває офіційна повітряна тривога" in t and "укритті" in t
    db.set_siren_state("kyivska", False)
    db.set_siren_state("lvivska", True)
    assert "тривоги немає" in voice.voice_text(db, 1, now=int(time.time()))


def test_voice_synthesis_failure_returns_none(monkeypatch):
    import sys
    from airradar.product import voice
    monkeypatch.setitem(sys.modules, "edge_tts", None)   # імпорт падає → None, без винятку
    assert asyncio.run(voice.synthesize("x")) is None
