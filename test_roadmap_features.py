# -*- coding: utf-8 -*-
"""Тесты недостающих пунктов роадмапа:

* regions.py: без опечатки в ключах, Чернівецька определяется;
* city_coords: центроид chernivetska + region_for_point;
* карты (map_live.html, miniapp/map.html): chernivetska в координатах/именах;
* bot_ui: «Моя зона» (pending+TTL+StopPropagation), «Укриття», «📈 Тиждень»,
  групповые /start и /city;
* main: received_ts → замер pipeline latency, фильтр «Укриття» и маркер
  «Торкнеться вашої зони» в рассылке;
* sirens: толерантный парсер active_alert_slugs + состояния сирен в /status;
* wave_forecast.accuracy_post_text: недельный пост точности;
* ai_summarizer: SummaryCache/CachedSummarizer + эскалация Groq;
* config: новые флаги окружения.
"""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon import events

from database import Database
from aggregator import PendingAlert
from incident_fusion import IncidentFact

BASE = Path(__file__).resolve().parent


def _fact(region: str = "sumska", weapon: str = "uav") -> IncidentFact:
    return IncidentFact(
        weapon_class=weapon, stage="imminent", origin_region=region,
        destination_region=region, count_kind="", count_value=None,
        is_delta=False, raw_designation="",
    )


class FakePublisher:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return {"message_id": len(self.sent), "chat": {"id": -100}}

    async def edit(self, chat_id, message_id, text):
        self.sent.append(text)
        return {"message_id": message_id, "chat": {"id": chat_id}}


class FakeNotifyClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, user_id, text, link_preview=False, buttons=None):
        self.sent.append((user_id, text))


# =====================================================================
#  regions / city_coords / карты
# =====================================================================

class RegionsAndCoordsTests(unittest.TestCase):
    def test_no_typo_key_in_chernivetska(self):
        from regions import REGIONS
        keys = REGIONS["chernivetska"][1]
        self.assertNotIn("бук?=овин", keys)
        self.assertIn("буковин", keys)

    def test_chernivetska_detected(self):
        from regions import detect_region
        self.assertIn("chernivetska", detect_region("тривога у Чернівцях"))
        self.assertIn("chernivetska", detect_region("БПЛА над Буковиною"))

    def test_centroid_and_region_for_point(self):
        from city_coords import REGION_CENTROIDS, region_for_point
        self.assertEqual(REGION_CENTROIDS.get("chernivetska"), (48.29, 25.94))
        self.assertEqual(region_for_point(48.29, 25.94), "chernivetska")
        self.assertEqual(region_for_point(50.45, 30.52), "kyivska")
        self.assertEqual(region_for_point(46.48, 30.73), "odeska")

    def test_map_files_have_chernivetska(self):
        for name in ("map_live.html", os.path.join("miniapp", "map.html")):
            raw = (BASE / name).read_text(encoding="utf-8")
            self.assertIn("chernivetska: [48.29, 25.94]", raw, name)
            self.assertIn("chernivetska: 'Буковина'", raw, name)


# =====================================================================
#  bot_ui: Моя зона / Укриття / Тиждень / группы
# =====================================================================

def _handler_by_name(bot, name):
    for _args, _kwargs, fn in bot.handlers:
        if getattr(fn, "__name__", "") == name:
            return fn
    raise AssertionError(f"handler {name} not found")


class FakeGeoEvent:
    def __init__(self, uid, lat, lon, private=True):
        self.sender_id = uid
        self.is_private = private
        self.message = SimpleNamespace(geo=SimpleNamespace(lat=lat, long=lon))
        self.responses = []

    async def respond(self, text, parse_mode=None, buttons=None):
        self.responses.append(text)


class FakeMsgEvent:
    def __init__(self, uid, text="", pattern=None, private=True, chat_id=None):
        self.sender_id = uid
        self.is_private = private
        self.chat_id = chat_id if chat_id is not None else uid
        self.text = text
        self.pattern_match = pattern
        self.responses = []
        self.buttons = []

    async def respond(self, text, parse_mode=None, buttons=None):
        self.responses.append(text)
        self.buttons.append(buttons)

    class _M:
        def group(self, idx):
            return ""

    @staticmethod
    def pm(group1=""):
        m = SimpleNamespace()
        m.group = lambda i: group1 if i == 1 else ""
        return m


class BotUiRoadmapTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        from bot_ui import register_handlers
        self.bot = _FakeBot()
        register_handlers(self.bot, self.db, 111)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _callback(self, data, sender_id=111):
        from test_bot_ui import FakeCallbackEvent
        event = FakeCallbackEvent(data.encode("utf-8"), sender_id)
        asyncio.run(self.bot.dispatch_callback(event))
        return event

    def test_main_menu_has_zone_and_shelter(self):
        event = self._callback("main")
        datas = _button_datas(event.edits[-1]["buttons"])
        self.assertIn("myzone", datas)
        self.assertIn("shelter", datas)

    def test_myzone_sets_pending_and_prompts(self):
        event = self._callback("myzone")
        deadline = self.bot._airradar_zone_pending.get(111, 0)
        self.assertGreater(deadline, time.time() + 500)
        self.assertIn("Моя зона", event.responses[-1])

    def test_zone_geo_saves_home_region_and_stops(self):
        from city_coords import region_for_point
        uid = 222
        self.bot._airradar_zone_pending = {uid: time.time() + 600}
        event = FakeGeoEvent(uid, 48.29, 25.94)
        with self.assertRaises(events.StopPropagation):
            asyncio.run(_handler_by_name(self.bot, "_zone_geo")(event))
        self.assertEqual(self.db.get_home_region(uid), region_for_point(48.29, 25.94))
        self.assertNotIn(uid, self.bot._airradar_zone_pending)
        self.assertTrue(event.responses)

    def test_zone_geo_without_pending_passes_through(self):
        event = FakeGeoEvent(333, 48.29, 25.94)
        # Нет StopPropagation — событие уходит дальше (geo_report).
        asyncio.run(_handler_by_name(self.bot, "_zone_geo")(event))
        self.assertFalse(event.responses)
        self.assertEqual(self.db.get_home_region(333), "")

    def test_zone_geo_expired_ttl_passes_through(self):
        uid = 444
        self.bot._airradar_zone_pending = {uid: time.time() - 10}
        event = FakeGeoEvent(uid, 48.29, 25.94)
        asyncio.run(_handler_by_name(self.bot, "_zone_geo")(event))
        self.assertFalse(event.responses)
        self.assertEqual(self.db.get_home_region(uid), "")

    def test_shelter_toggle_flow(self):
        opened = self._callback("shelter")
        self.assertIn("вимкнено", opened.edits[-1]["text"])
        self.assertFalse(self.db.get_shelter_mode(111))
        on = self._callback("sht")
        self.assertTrue(self.db.get_shelter_mode(111))
        self.assertIn("увімкнено", on.edits[-1]["text"])
        off = self._callback("sht")
        self.assertFalse(self.db.get_shelter_mode(111))
        self.assertIn("вимкнено", off.edits[-1]["text"])

    def test_week_text_bars(self):
        from bot_ui import _week_text
        now = int(time.time())
        for i in range(3):  # 3 эпизода сегодня
            self.db.alert_start("sumska", event_ts=now - 3600 - i * 60)
            self.db.alert_end("sumska", event_ts=now - 3000 - i * 60)
        text = _week_text(self.db, "sumska")
        self.assertIn("тривоги за 7 днів", text)
        self.assertIn("Всього епізодів: <b>3</b>", text)
        self.assertTrue(set("▁▂▃▄▅▆▇█") & set(text))

    def test_week_button_in_region_menu_and_callback(self):
        from bot_ui import _region_menu_kb
        datas = _button_datas(_region_menu_kb("sumska"))
        self.assertIn("rwk:sumska", datas)
        event = self._callback("rwk:sumska")
        self.assertIn("тривоги за 7 днів", event.edits[-1]["text"])

    def test_group_start_greeting_no_menu(self):
        event = FakeMsgEvent(-100555, "/start", private=False)
        asyncio.run(_handler_by_name(self.bot, "_start")(event))
        self.assertEqual(len(event.responses), 1)
        self.assertIn("особистих повідомленнях", event.responses[0])
        self.assertIn("/city", event.responses[0])
        self.assertIsNone(event.buttons[0])  # без меню-кнопок

    def test_group_city_subscribes_chat_id(self):
        event = FakeMsgEvent(777, "/city Київ", pattern=FakeMsgEvent.pm("Київ"),
                             private=False, chat_id=-100777)
        asyncio.run(_handler_by_name(self.bot, "_city")(event))
        self.assertTrue(self.db.is_subscribed(-100777, "kyivska"))
        self.assertFalse(self.db.is_subscribed(777, "kyivska"))
        self.assertIn("Чат підписано", event.responses[0])
        # Повторная команда отписывает чат.
        asyncio.run(_handler_by_name(self.bot, "_city")(event))
        self.assertFalse(self.db.is_subscribed(-100777, "kyivska"))


def _button_datas(buttons) -> list[str]:
    datas = []
    for row in buttons or []:
        for btn in row:
            data = getattr(btn, "data", None)
            if data is None:
                data = getattr(getattr(btn, "type", None), "data", None)
            if data is None:
                continue
            datas.append(data.decode("utf-8") if isinstance(data, bytes) else data)
    return datas


class _FakeBot:
    """Минимальный сборщик хендлеров (как в test_bot_ui.FakeBot)."""

    def __init__(self):
        self.handlers = []

    def on(self, *args, **kwargs):
        def deco(fn):
            self.handlers.append((args, kwargs, fn))
            return fn
        return deco

    def callback_handlers(self):
        return [fn for _a, _k, fn in self.handlers if getattr(fn, "__name__", "") == "_callback"]

    async def dispatch_callback(self, event):
        for fn in self.callback_handlers():
            await fn(event)


# =====================================================================
#  main: latency, Укриття, маркер зоны
# =====================================================================

class PipelineLatencyTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        import main as _main
        self.main = _main

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_publish_items_records_latency(self):
        now = int(time.time())
        items = [
            PendingAlert(text="т", source="s", event_ts=now, fact=_fact(),
                         confirmation={}, regions=["sumska"], received_ts=now - 20),
            PendingAlert(text="т2", source="s2", event_ts=now, fact=_fact(),
                         confirmation={}, regions=["sumska"], received_ts=now - 5),
        ]
        asyncio.run(self.main._publish_items(self.db, FakePublisher(), None, items))
        lat = self.db.latency_percentiles(seconds=60)
        self.assertEqual(lat["samples"], 1)
        # От самого старого received_ts: ~20 с (минус доля секунды на выполнение).
        self.assertGreaterEqual(lat["p50_ms"], 19500)

    def test_publish_items_skips_legacy_without_received_ts(self):
        now = int(time.time())
        items = [PendingAlert(text="т", source="s", event_ts=now, fact=_fact(),
                              confirmation={}, regions=[])]
        asyncio.run(self.main._publish_items(self.db, FakePublisher(), None, items))
        self.assertEqual(self.db.latency_percentiles(seconds=60)["samples"], 0)


class NotifyShelterZoneTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        import main as _main
        self.main = _main
        self.db.subscribe(7, "sumska")
        self.client = FakeNotifyClient()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def test_shelter_silences_noncritical(self):
        self.db.set_shelter_mode(7, True)
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["sumska"], "🛸 БпЛА у Сумах", weapon_class="uav"))
        self.assertEqual(self.client.sent, [])

    def test_shelter_passes_critical_and_standdown(self):
        self.db.set_shelter_mode(7, True)
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["sumska"], "🚀 Балістика", weapon_class="ballistic"))
        self.assertEqual(len(self.client.sent), 1)
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["sumska"], "🟢 ВІДБІЙ Сумська обл.", weapon_class=""))
        self.assertEqual(len(self.client.sent), 2)

    def test_zone_marker_added_and_beats_shelter(self):
        self.db.set_home_region(7, "sumska")
        self.db.set_shelter_mode(7, True)
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["sumska"], "🛸 БпЛА у Сумах", weapon_class="uav"))
        self.assertEqual(len(self.client.sent), 1)
        self.assertTrue(self.client.sent[0][1].startswith("🎯 Торкнеться вашої зони"))

    def test_zone_marker_via_city_in_text(self):
        # Регион рассылки другой, но текст честно называет город зоны пользователя.
        self.db.set_home_region(7, "chernivetska")
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["vinnytska"],
            "🛸 БпЛА: цілі курсом на Чернівці", weapon_class="uav"))
        # Пользователь подписан на sumska, а рассылка — по vinnytska: не получит.
        self.assertEqual(self.client.sent, [])
        self.db.subscribe(7, "vinnytska")
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["vinnytska"],
            "🛸 БпЛА: цілі курсом на Чернівці", weapon_class="uav"))
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("🎯 Торкнеться вашої зони", self.client.sent[0][1])

    def test_no_marker_without_home(self):
        asyncio.run(self.main._notify_subscribers(
            self.client, self.db, ["sumska"], "🛸 БпЛА у Сумах", weapon_class="uav"))
        self.assertEqual(len(self.client.sent), 1)
        self.assertNotIn("🎯", self.client.sent[0][1])


# =====================================================================
#  sirens + /status
# =====================================================================

class SirensTests(unittest.TestCase):
    def test_active_alert_slugs_mapping_and_tolerance(self):
        from sirens import active_alert_slugs
        payload = {
            "alerts": [
                {"region_id": 21, "alert_type": "air_raid"},           # Сумська
                {"region_id": 999, "region_name": "Харківська область"},  # фолбэк по имени
                {"region_id": 8, "alert_type": "recovery"},            # не сирена
                {"region_id": 5},                                      # без типа — считаем сиреной
                "мусор",
            ]
        }
        slugs = active_alert_slugs(payload)
        self.assertEqual(slugs, {"sumska", "kharkivska", "dnipropetrovska"})

    def test_active_alert_slugs_empty_payloads(self):
        from sirens import active_alert_slugs
        self.assertEqual(active_alert_slugs({}), set())
        self.assertEqual(active_alert_slugs({"alerts": []}), set())
        self.assertEqual(active_alert_slugs(None), set())

    def test_db_siren_states_and_fresh(self):
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            db.set_siren_state("sumska", True)
            db.set_siren_state("kyivska", True)
            db.set_siren_state("odeska", False)
            self.assertEqual(set(db.siren_states_active()), {"sumska", "kyivska"})
            self.assertEqual(set(db.siren_states_fresh(max_age_sec=900)), {"sumska", "kyivska"})
            self.assertEqual(db.siren_states_fresh(max_age_sec=-1), [])
        finally:
            db.close()
            os.unlink(file.name)

    def test_status_text_contains_latency_and_sirens(self):
        from admin_ui import _status_text
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            db.channel_seen("@src", "military")  # чтобы была хоть одна строка каналов
            db.record_pipeline_latency(1500)
            db.record_pipeline_latency(9000)
            db.set_siren_state("sumska", True)
            text = _status_text(db)
            self.assertIn("Конвейер (24г)", text)
            self.assertTrue("1,5 с" in text or "1.5 с" in text)
            self.assertIn("сирены", text)
            self.assertIn("sumska", text)
        finally:
            db.close()
            os.unlink(file.name)

    def test_status_text_stale_sirens_hint(self):
        from admin_ui import _status_text
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            db.channel_seen("@src", "military")
            db.set_siren_state("sumska", True)
            # Состарим updated_ts вручную.
            with db._lock:
                db._conn.execute("UPDATE siren_states SET updated_ts = ?", (int(time.time()) - 3600,))
                db._conn.commit()
            text = _status_text(db)
            self.assertIn("устарели", text)
        finally:
            db.close()
            os.unlink(file.name)


class AccuracyPostTests(unittest.TestCase):
    def test_accuracy_post_text_empty_db(self):
        import wave_forecast
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            text = wave_forecast.accuracy_post_text(db)
            self.assertIn("Точність прогнозів", text)
            self.assertIn("недостатньо даних", text)
        finally:
            db.close()
            os.unlink(file.name)

    def test_accuracy_post_text_with_episodes(self):
        import wave_forecast
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        try:
            base = int(time.time()) - 2 * 86400
            for i, gap in enumerate([1200, 1200, 1200, 1200]):
                db.add_event(event_ts=base + i * 21600, weapon_class="uav",
                             stage="movement", region="sumska", text="БпЛА", source="t")
                db.alert_start("sumska", event_ts=base + i * 21600 - 300)
                db.alert_end("sumska", event_ts=base + i * 21600 + gap)
            text = wave_forecast.accuracy_post_text(db)
            self.assertIn("Відбій у межах медіани", text)
            self.assertIn("100%", text)
        finally:
            db.close()
            os.unlink(file.name)

    def test_accuracy_post_loop_posts_once_and_dedups(self):
        import main as _main
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        db = Database(file.name)
        pub = FakePublisher()
        try:
            kdt = _main._kyiv_datetime()
            task = asyncio.run(asyncio.wait_for(
                self._run_loop(_main, db, pub, kdt.weekday(), kdt.hour), timeout=10))
            self.assertEqual(len(pub.sent), 1)
            self.assertIn("Точність прогнозів", pub.sent[0])
        finally:
            db.close()
            os.unlink(file.name)

    @staticmethod
    async def _run_loop(main_mod, db, pub, day, hour):
        task = asyncio.ensure_future(main_mod._accuracy_post_loop(
            db, pub, day=day, hour=hour, check_interval=0.05))
        await asyncio.sleep(0.3)  # несколько проходов внутри одного часа — дедуп по дате
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# =====================================================================
#  ai_summarizer: SummaryCache + эскалация
# =====================================================================

class SummaryCacheTests(unittest.TestCase):
    def test_roundtrip_and_ttl_off(self):
        from ai_summarizer import SummaryCache
        off = SummaryCache(ttl=0)
        off.put("текст", "резюме")
        self.assertIsNone(off.get("текст"))
        cache = SummaryCache(ttl=100)
        cache.put("текст", "резюме")
        self.assertEqual(cache.get("текст"), "резюме")
        self.assertIsNone(cache.get("другой текст"))

    def test_expired_entry_dropped(self):
        from ai_summarizer import SummaryCache
        cache = SummaryCache(ttl=10)
        cache.put("текст", "резюме")
        key = cache._key("текст")
        ts, value = cache._store[key]
        cache._store[key] = (ts - 100, value)
        self.assertIsNone(cache.get("текст"))

    def test_max_size_evicts_oldest(self):
        from ai_summarizer import SummaryCache
        cache = SummaryCache(ttl=100, max_size=2)
        cache.put("a", "1")
        time.sleep(0.01)
        cache.put("b", "2")
        time.sleep(0.01)
        cache.put("c", "3")  # вытесняет "a" (самый старый)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), "2")
        self.assertEqual(cache.get("c"), "3")


class CachedSummarizerTests(unittest.TestCase):
    def test_caches_success_not_fallback(self):
        from ai_summarizer import CachedSummarizer, SummaryCache

        class Inner:
            def __init__(self):
                self.calls = 0
                self.answer = "КОРОТКО"

            async def summarize(self, text):
                self.calls += 1
                return self.answer if self.answer else text

            async def classify(self, text, system_prompt, max_tokens=30):
                return "cls"

            async def healthcheck(self):
                return True

            async def aclose(self):
                pass

        inner = Inner()
        wrapped = CachedSummarizer(inner, SummaryCache(ttl=100))
        self.assertEqual(asyncio.run(wrapped.summarize("текст")), "КОРОТКО")
        self.assertEqual(asyncio.run(wrapped.summarize("текст")), "КОРОТКО")
        self.assertEqual(inner.calls, 1)  # второй раз — из кэша

        inner.answer = ""  # fallback: возвращает исходник, в кэш не пишется
        self.assertEqual(asyncio.run(wrapped.summarize("другое"), ), "другое")
        self.assertEqual(asyncio.run(wrapped.summarize("другое")), "другое")
        self.assertEqual(inner.calls, 3)

    def test_passthrough(self):
        from ai_summarizer import CachedSummarizer, SummaryCache

        class Inner:
            async def summarize(self, text):
                return text

            async def classify(self, text, system_prompt, max_tokens=30):
                return "ok"

            async def healthcheck(self):
                return True

            async def aclose(self):
                self.closed = True

        inner = Inner()
        wrapped = CachedSummarizer(inner, SummaryCache(ttl=100))
        self.assertEqual(asyncio.run(wrapped.classify("x", "sys")), "ok")
        self.assertTrue(asyncio.run(wrapped.healthcheck()))
        asyncio.run(wrapped.aclose())


class GroqEscalationTests(unittest.TestCase):
    def _gs(self, strong="strong-model"):
        from ai_summarizer import GroqSummarizer
        return GroqSummarizer(api_key="k", model="lite", timeout=5, strong_model=strong)

    def test_escalation_on_degraded(self):
        gs = self._gs()
        calls = []

        async def fake_once(text, model):
            calls.append(model)
            if model == "lite":
                return text, True  # деградация лёгкой модели
            return "РЕЗЮМЕ", False

        gs._summarize_once = fake_once
        self.assertEqual(asyncio.run(gs.summarize("текст")), "РЕЗЮМЕ")
        self.assertEqual(calls, ["lite", "strong-model"])

    def test_no_escalation_on_good_answer(self):
        gs = self._gs()
        calls = []

        async def fake_once(text, model):
            calls.append(model)
            return "РЕЗЮМЕ", False

        gs._summarize_once = fake_once
        self.assertEqual(asyncio.run(gs.summarize("текст")), "РЕЗЮМЕ")
        self.assertEqual(calls, ["lite"])

    def test_no_strong_model_configured(self):
        gs = self._gs(strong="")
        calls = []

        async def fake_once(text, model):
            calls.append(model)
            return text, True

        gs._summarize_once = fake_once
        self.assertEqual(asyncio.run(gs.summarize("текст")), "текст")
        self.assertEqual(calls, ["lite"])

    def test_make_summarizer_wraps_in_cache(self):
        from ai_summarizer import CachedSummarizer, GroqSummarizer, make_summarizer
        settings = SimpleNamespace(
            ollama_url="http://x", ollama_model="m", http_timeout=5,
            groq_api_key="k", groq_url="http://x", groq_model="lite",
            groq_model_strong="strong", summary_cache_ttl=100, summary_cache_size=64,
        )
        wrapped = make_summarizer("groq", settings, None)
        self.assertIsInstance(wrapped, CachedSummarizer)
        self.assertIsInstance(wrapped._inner, GroqSummarizer)
        self.assertEqual(wrapped._inner._strong_model, "strong")

        settings.summary_cache_ttl = 0
        bare = make_summarizer("groq", settings, None)
        self.assertIsInstance(bare, GroqSummarizer)


# =====================================================================
#  config: новые флаги
# =====================================================================

class ConfigFlagsTests(unittest.TestCase):
    def test_settings_defaults(self):
        from config import Settings
        s = Settings(tg_api_id=1, tg_api_hash="h", session_name="s",
                     bot_token="t", target_channel="@c")
        self.assertEqual(s.alerts_in_ua_token, "")
        self.assertTrue(s.accuracy_post_enabled)
        self.assertEqual(s.accuracy_post_weekday, 1)
        self.assertEqual(s.accuracy_post_hour, 9)
        self.assertEqual(s.groq_model_strong, "llama-3.3-70b-versatile")
        self.assertEqual(s.summary_cache_ttl, 900)
        self.assertEqual(s.summary_cache_size, 512)

    def test_load_settings_parses_new_env(self):
        import config
        from unittest.mock import patch
        env = {
            "TG_API_ID": "1", "TG_API_HASH": "h", "BOT_TOKEN": "t",
            "TARGET_CHANNEL": "@c", "SOURCE_CHANNELS": "@a",
            "ALERTS_IN_UA_TOKEN": "tok", "ACCURACY_POST": "0",
            "ACCURACY_POST_DAY": "5", "ACCURACY_POST_HOUR": "18",
            "GROQ_MODEL_STRONG": "custom-strong", "SUMMARY_CACHE_TTL": "0",
            "SUMMARY_CACHE_SIZE": "8",
        }
        with patch.dict(os.environ, env):
            s = config.load_settings()
        self.assertEqual(s.alerts_in_ua_token, "tok")
        self.assertFalse(s.accuracy_post_enabled)
        self.assertEqual(s.accuracy_post_weekday, 5)
        self.assertEqual(s.accuracy_post_hour, 18)
        self.assertEqual(s.groq_model_strong, "custom-strong")
        self.assertEqual(s.summary_cache_ttl, 0)
        self.assertEqual(s.summary_cache_size, 8)


if __name__ == "__main__":
    unittest.main()
