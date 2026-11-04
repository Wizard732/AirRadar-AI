# -*- coding: utf-8 -*-
"""Golden-тести конвеєра «пост джерела → подія → канал/ЛС» (етап 1–2 плану аудиту).

Тести розділені на: (1) поведінку, що ГАРАНТУЄТЬСЯ; (2) підтверджені аудитом дефекти —
вони зафіксовані як xfail(strict=True): коли дефект виправлять, тест почне проходити і
змусить прибрати мітку.
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from track_engine import Risk

from aggregator import AlertAggregator
from database import Database
from dedup import DedupCache
from test_critical_confirm import FakeSummarizer, _event, _import_main

main = None


class Pub:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text, *a, **kw):
        self.sent.append(text)
        return None if self.fail else {"chat": {"id": "c"}, "message_id": len(self.sent)}

    async def edit(self, *a, **kw):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        global main
        main = _import_main()
        self.f = tempfile.NamedTemporaryFile(delete=False); self.f.close()
        self.db = Database(self.f.name)
        main._DEDUP = type(main._DEDUP)()
        self.notified = []
        self._orig = main._notify_subscribers

        async def fake_notify(bot, db, regions, text, **kw):
            self.notified.append((list(regions), text))
        main._notify_subscribers = fake_notify

    def tearDown(self):
        main._notify_subscribers = self._orig
        self.db.close(); os.unlink(self.f.name)

    def run_posts(self, posts, pub, window=0.05):
        """posts: [(text, ts, source)] — один агрегатор і DedupCache на всі пости, як у проді."""
        async def go():
            async def flush(items):
                await main._publish_items(self.db, pub, object(), items)
            agg = AlertAggregator(window, flush, critical_needs_confirmation=True)
            dedup = DedupCache(ttl=60)
            for text, ts, src in posts:
                await main._process_message(_event(text, ts, src), FakeSummarizer(), pub, dedup, self.db, bot_client=object(), aggregator=agg)
            await asyncio.sleep(window * 4)
            await agg.flush_all() if hasattr(agg, "flush_all") else None
        asyncio.run(go())

    def now(self, m=1):
        return datetime.now(timezone.utc) - timedelta(minutes=m)


class GoldenTests(Base):
    def test_unofficial_single_source_uav_published_labelled_unconfirmed(self):
        pub = Pub()
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(), "chan_a")], pub)
        self.assertEqual(len(pub.sent), 1)
        self.assertIn("❔", pub.sent[0]); self.assertNotIn("✅", pub.sent[0])
        self.assertIn("Бровари", pub.sent[0])
        self.assertTrue(self.notified, "підписникам регіону йде розсилка")

    def test_same_text_twice_creates_one_post(self):
        pub = Pub(); t = self.now()
        self.run_posts([("БпЛА курсом на Бровари", t, "chan_a"), ("БпЛА курсом на Бровари", t, "chan_b")], pub)
        self.assertEqual(len(pub.sent), 1)

    def test_news_and_unknown_weapon_never_become_alerts(self):
        pub = Pub()
        self.run_posts([("Сьогодні в Києві відкрили новий парк для дітей", self.now(), "chan_a")], pub)
        self.assertEqual(pub.sent, []); self.assertEqual(self.notified, [])

    def test_disabled_source_is_ignored(self):
        pub = Pub(); self.db.channel_seen("bad_chan", "military"); self.db.disable_channel("bad_chan")
        self.assertTrue(self.db.is_channel_disabled("bad_chan", "military"))
        self.run_posts([("БпЛА курсом на Бровари", self.now(), "bad_chan")], pub)
        self.assertEqual(pub.sent, [])

    def test_silence_does_not_close_alert(self):
        """Відсутність нових постів не знімає тривогу (лише явний відбій)."""
        self.db.alert_start("kyivska", event_ts=int(self.now(30).timestamp()))
        self.assertIn("kyivska", self.db.active_alert_regions())
        self.assertEqual(self.db.expire_stale_alerts(max_age_seconds=6 * 3600), 0)
        self.assertIn("kyivska", self.db.active_alert_regions())

    def test_official_start_then_end_published_once_each(self):
        pub = Pub()
        self.run_posts([("🔴 Київська область — повітряна тривога", self.now(2), "chan_a"),
                        ("🟢 Київська область — відбій повітряної тривоги", self.now(1), "chan_a")], pub)
        starts = [s for s in pub.sent if "ПОВІТРЯНА ТРИВОГА" in s]
        ends = [s for s in pub.sent if "ВІДБІЙ ТРИВОГИ" in s]
        self.assertEqual((len(starts), len(ends)), (1, 1), pub.sent)

    def test_end_without_open_alert_is_not_published(self):
        """«Відбій» без відкритої тривоги не створює фіктивну подію для каналу."""
        pub = Pub()
        self.run_posts([("🟢 Київська область — відбій повітряної тривоги", self.now(1), "chan_a")], pub)
        self.assertEqual([s for s in pub.sent if "ВІДБІЙ" in s], [])


class FixedDefects(Base):
    def test_dm_still_sent_when_channel_send_fails(self):
        """D1: збій каналу не скасовує розсилку в ЛС."""
        pub = Pub(fail=True)
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(), "chan_a")], pub)
        self.assertTrue(self.notified, "ЛС не залежить від успіху каналу")

    def test_failed_send_does_not_poison_publish_dedup(self):
        """D2: після невдалої відправки повтор повідомлення не відкидається як «уже опубліковане»."""
        pub = Pub(fail=True)
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(2), "chan_a")], pub)
        pub.fail = False
        self.run_posts([("Київщина: БпЛА курсом на Бровари і Бориспіль", self.now(1), "chan_b")], pub)
        self.assertTrue(any("Бровари" in t for t in pub.sent[1:]), pub.sent)
        self.assertEqual(len(pub.sent), 2)


def test_publish_dedup_reserve_release():
    from types import SimpleNamespace
    from airradar.processing.publish_dedup import PublishDedup
    d = PublishDedup()
    t = d.reserve("stand_down", None, [], now=1000)
    assert t is not None and d.reserve("stand_down", None, [], now=1100) is None
    d.release(t); assert d.reserve("stand_down", None, [], now=1100) is not None
    t = d.reserve("ballistic", None, ["kyivska"], now=1000)
    assert d.allow("ballistic", None, ["kyivska"], now=1100) is False
    d.release(t); assert d.allow("ballistic", None, ["kyivska"], now=1100) is True
    obs = [SimpleNamespace(oblast="kyivska", place="Бровари", relation="toward")]
    p = SimpleNamespace(kind="track", observations=obs)
    t = d.reserve("uav", p, ["kyivska"], now=1000)
    assert d.reserve("uav", p, ["kyivska"], now=1010) is None
    d.release(t); assert d.reserve("uav", p, ["kyivska"], now=1010) is not None


# ---- D3: читання екрана статистики не закриває тривоги ----
def test_stats_screen_does_not_expire_alerts(tmp_path):
    import time
    from airradar.ui.views import _region_stats_text
    db = Database(str(tmp_path / "a.db"))
    db.alert_start("kyivska", event_ts=int(time.time()) - 8 * 3600)
    _region_stats_text(db, "kyivska")
    assert "kyivska" in db.active_alert_regions(), "читання не має закривати тривогу"
    assert db.expire_stale_alerts(6 * 3600) == 1        # закриває лише явний фоновий цикл
    assert "kyivska" not in db.active_alert_regions()
    db.close()


def test_expire_alerts_loop_closes_with_log(tmp_path, caplog):
    import logging, time
    from airradar.runtime.background import expire_alerts_loop
    db = Database(str(tmp_path / "b.db"))
    db.alert_start("kyivska", event_ts=int(time.time()) - 8 * 3600)

    async def go():
        t = asyncio.create_task(expire_alerts_loop(db, interval=0))
        await asyncio.sleep(0.05); t.cancel()
        try: await t
        except asyncio.CancelledError: pass
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert db.active_alert_regions() == []
    assert any("не відбій" in r.getMessage() for r in caplog.records)
    db.close()


# ---- R1: буфер агрегатора переживає крах ----
def _pending(src="a", ts=None, key="k1", weapon="uav", dest="kyivska"):
    import time
    from aggregator import PendingAlert
    from incident_fusion import IncidentFact
    ts = ts or int(time.time())
    return PendingAlert(text="БпЛА курсом на Бровари", source=src, event_ts=ts,
                        fact=IncidentFact(weapon, "imminent", "", dest, "unspecified", None, False, ""),
                        confirmation={"status": "reported", "key": key}, regions=[dest], received_ts=ts)


def test_aggregator_buffer_survives_crash(tmp_path):
    from aggregator import AlertAggregator
    from airradar.processing.agg_store import AggStore
    db = Database(str(tmp_path / "c.db")); store = AggStore(db)

    async def crash():                      # «процес» отримав алерт, але впав до закінчення вікна
        agg = AlertAggregator(60, lambda items: asyncio.sleep(0), store=store)
        await agg.submit(_pending())
        assert len(store.load(600)) == 1    # у журналі
        for t in list(agg._timers.values()): t.cancel()   # без aclose(): жорсткий крах
    asyncio.run(crash())

    flushed = []
    async def restart():
        async def flush(items): flushed.append(items)
        agg = AlertAggregator(60, flush, store=AggStore(db))
        n = await agg.recover(600)
        for t in list(agg._timers.values()): t.cancel()
        await agg.aclose()
        return n
    n = asyncio.run(restart())
    assert n == 1 and len(flushed) == 1 and flushed[0][0].fact.destination_region == "kyivska"
    assert AggStore(db).load(600) == [], "після публікації записи видалено"
    db.close()


def test_recover_skips_stale_and_already_published(tmp_path):
    import time
    from aggregator import AlertAggregator
    from airradar.processing.agg_store import AggStore
    db = Database(str(tmp_path / "d.db")); store = AggStore(db)
    store.save(_pending(key="old"))
    with db._lock:
        db._conn.execute("UPDATE agg_pending SET created_ts=?", (int(time.time()) - 3600,)); db._conn.commit()
    store.save(_pending(key="dup")); store.save(_pending(key="fresh", dest="sumska"))
    flushed = []
    async def go():
        async def flush(items): flushed.extend(items)
        agg = AlertAggregator(0.05, flush, store=store)
        n = await agg.recover(600, already_published=lambda a: a.confirmation["key"] == "dup")
        await agg.aclose(); return n
    assert asyncio.run(go()) == 1
    assert [a.confirmation["key"] for a in flushed] == ["fresh"]
    assert store.load(3600 * 5) == []
    db.close()


def test_agg_store_corrupt_record_removed(tmp_path):
    from airradar.processing.agg_store import AggStore
    import time
    db = Database(str(tmp_path / "e.db")); store = AggStore(db)
    with db._lock:
        db._conn.execute("INSERT INTO agg_pending(created_ts,payload) VALUES (?,?)", (int(time.time()), "{не json")); db._conn.commit()
    assert store.load(600) == []
    assert db._conn.execute("SELECT COUNT(*) c FROM agg_pending").fetchone()["c"] == 0
    db.close()


# ---- R2/R3: збій обробки не губить пост ----
class InboxTests(Base):
    def _boom_once(self):
        """Змушує перший виклик merge_incident_fact падати (збій між fusion і submit)."""
        orig = self.db.merge_incident_fact
        state = {"n": 0}

        def flaky(*a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("тимчасовий збій БД")
            return orig(*a, **kw)
        self.db.merge_incident_fact = flaky

    def test_failure_keeps_inbox_row_and_forgets_dedup(self):
        from airradar.processing.inbox import Inbox
        self._boom_once(); pub = Pub()
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(), "chan_a")], pub)
        self.assertEqual(pub.sent, [])
        rows = self.db._conn.execute("SELECT attempts, last_error FROM ingest_inbox").fetchall()
        self.assertEqual(len(rows), 1); self.assertIn("збій БД", rows[0]["last_error"])
        # R3: той самий текст, надісланий знову, НЕ відкидається як дубль
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(), "chan_a")], pub)
        self.assertEqual(len(pub.sent), 1)
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) c FROM ingest_inbox").fetchone()["c"], 1, "старий запис чекає retry-циклу")

    def test_retry_loop_reprocesses_failed_post(self):
        from airradar.processing.inbox import Inbox
        from airradar.runtime.background import inbox_retry_loop
        self._boom_once(); pub = Pub()
        self.run_posts([("Київщина: БпЛА курсом на Бровари", self.now(), "chan_a")], pub)
        inbox = Inbox.of(self.db)
        self.db._conn.execute("UPDATE ingest_inbox SET failed_ts = failed_ts - 60"); self.db._conn.commit()
        seen = []

        async def go():
            async def process(text, source, event_ts, inbox_id):
                seen.append((text, inbox_id))
                async def flush(items): await main._publish_items(self.db, pub, object(), items)
                agg = AlertAggregator(0.05, flush)
                ev = _event(text, datetime.fromtimestamp(event_ts, timezone.utc), source)
                await main._process_message(ev, FakeSummarizer(), pub, DedupCache(ttl=60), self.db, bot_client=object(), aggregator=agg, inbox_id=inbox_id)
                await asyncio.sleep(0.3)
            t = asyncio.create_task(inbox_retry_loop(self.db, process, interval=0))
            await asyncio.sleep(0.8); t.cancel()
            try: await t
            except asyncio.CancelledError: pass
        asyncio.run(go())
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(pub.sent), 1, "після повтору пост опубліковано")
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) c FROM ingest_inbox").fetchone()["c"], 0)


def test_inbox_limits_attempts_and_age(tmp_path):
    import time
    from airradar.processing.inbox import Inbox, MAX_ATTEMPTS
    db = Database(str(tmp_path / "f.db")); ib = Inbox.of(db)
    i = ib.add("s", 1, "x"); ib.fail(i, "e")
    now = int(time.time()) + 10
    for _ in range(MAX_ATTEMPTS):
        assert [r["id"] for r in ib.claim_retry(now)] == [i]; ib.fail(i, "e"); now += 10
    assert ib.claim_retry(now) == [], "після MAX_ATTEMPTS запис видалено"
    j = ib.add("s", 1, "old"); ib.fail(j, "e")
    assert ib.claim_retry(int(time.time()) + 100000) == []
    db.close()


def test_notify_failure_of_one_user_does_not_block_others(tmp_path, monkeypatch):
    """R4: збій доставки одному отримувачу не зупиняє розсилку; розсилка паралельна."""
    import main as m
    from airradar.delivery import tg_delivery
    db = Database(str(tmp_path / "g.db"))
    for u in (1, 2, 3, 4):
        db.subscribe(u, "kyivska")
    calls, active, peak = [], [0], [0]

    async def deliver(bot, db_, key, user_id, text, **kw):
        active[0] += 1; peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05); active[0] -= 1; calls.append(user_id)
        if user_id == 2:
            raise RuntimeError("blocked")
        return True
    monkeypatch.setattr(tg_delivery, "deliver", deliver)
    asyncio.run(m._notify_subscribers(object(), db, ["kyivska"], "🛵 Київщина: БпЛА", feedback_key="k"))
    assert sorted(calls) == [1, 2, 3, 4] and peak[0] > 1, (calls, peak)
    db.close()


# ---- знахідки симуляції атаки (етап 22) ----
class SimulationFindings(Base):
    def test_ballistic_two_sources_with_different_phrasing_is_one_incident_and_published(self):
        """«Балістика на Київ» (stage unknown) + «Загроза балістики для Києва» (imminent) — одна подія, 2 джерела."""
        pub = Pub()
        self.run_posts([("Балістика на Київ", self.now(2), "src_a"), ("Загроза балістики для Києва", self.now(1), "src_b")], pub)
        ballistic = [s for s in pub.sent if "Балістика" in s]
        self.assertEqual(len(ballistic), 1, pub.sent)
        self.assertIn("✅ підтверджено 2 незалежними", ballistic[0])
        self.assertTrue(self.notified)

    def test_consequence_post_without_threat_words_reaches_impact_journal(self):
        """Пост про приліт/поранених раніше відсікався fast_filter до журналу наслідків."""
        pub = Pub()
        self.run_posts([("Харків: приліт по житловому будинку, 3 поранені, пожежа", self.now(), "src_a")], pub)
        rows = self.db._conn.execute("SELECT oblast, place, injured, kind FROM impact_reports").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["injured"]), ("hit", 3))


def test_risk_text_does_not_invent_distance_for_named_target():
    from track_engine import risk_text
    r = Risk(track_id="t", weapon="uav", level="imminent", distance_km=0.0, bearing_from_point=0.0, approaching=True, cpa_km=0.0,
             eta_min=0, speed_kmh=180.0, speed_source="typical", count=1, eta_range=(0, 1), approx_only=True)
    t = risk_text(r)
    assert "(~0 км)" not in t and ": 0 км" not in t and "0–1 хв" not in t and "точна позиція невідома" in t
