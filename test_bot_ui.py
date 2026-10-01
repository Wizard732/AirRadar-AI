import asyncio
import os
import re
import tempfile
import unittest

from admin_ui import register_admin_handlers
from bot_ui import register_handlers
from database import Database
from interests_ui import register_interests_handlers

ADMIN_ID = 111


def _pattern_source(pat):
    """Исходная строка regex из того, что хранит Telethon в билдере.

    Telethon сохраняет ``pattern=re.compile(...).match`` — связанный метод,
    исходная строка лежит в ``__self__.pattern``.
    """
    if pat is None or isinstance(pat, str):
        return pat
    bound = getattr(pat, "__self__", None)
    if bound is not None:
        return getattr(bound, "pattern", None)
    return getattr(pat, "pattern", None)


class FakeBot:
    """Собирает хендлеры вместо реального Telethon-роутинга."""

    def __init__(self):
        self.handlers = []  # (args, kwargs, fn)

    def on(self, *args, **kwargs):
        def deco(fn):
            self.handlers.append((args, kwargs, fn))
            return fn
        return deco

    def handlers_by_pattern(self, pattern: str):
        """Хендлеры NewMessage по исходной строке regex.

        В Telethon bot.on() получает инстанс билдера событий позиционным
        аргументом (без kwargs), поэтому pattern читаем из args[0].pattern.
        """
        out = []
        for args, kwargs, fn in self.handlers:
            pat = kwargs.get("pattern")
            if pat is None and args:
                pat = getattr(args[0], "pattern", None)
            if _pattern_source(pat) == pattern:
                out.append(fn)
        return out

    def callback_handlers(self) -> list:
        """Все CallbackQuery-хендлеры в порядке регистрации.

        Модули (bot_ui, interests_ui) вешают хендлеры с одинаковым именем
        _callback; в проде Telethon вызывает их все, здесь — то же самое.
        """
        return [
            fn for args, _kwargs, fn in self.handlers
            if getattr(fn, "__name__", "") == "_callback"
        ]

    async def dispatch_callback(self, event):
        for fn in self.callback_handlers():
            await fn(event)


class FakeCallbackEvent:
    def __init__(self, data: bytes, sender_id: int):
        self.data = data
        self.sender_id = sender_id
        self.edits = []
        self.answers = []
        self.responses = []
        self.respond_buttons = []  # buttons каждого respond-а (в порядке вызова)

    async def edit(self, text, parse_mode=None, buttons=None):
        self.edits.append({"text": text, "buttons": buttons})

    async def answer(self, text=None, alert=False):
        self.answers.append(text)

    async def respond(self, text, parse_mode=None, buttons=None):
        self.responses.append(text)
        self.respond_buttons.append(buttons)


class FakeMessageEvent:
    def __init__(self, text: str, sender_id: int):
        self.text = text
        self.sender_id = sender_id
        self.pattern_match = None
        self.responses = []
        self.buttons = []

    async def respond(self, text, parse_mode=None, buttons=None):
        self.responses.append(text)
        self.buttons.append(buttons)


def _button_rows(buttons):
    """Нормализация клавиатуры: каждая строка — список кнопок.

    Telethon принимает и плоские списки (элемент = одно-кнопочная строка),
    и списки списков; TL-типы и Button-обёртки сосуществуют в 1.45+.
    """
    for row in buttons or []:
        yield row if isinstance(row, (list, tuple)) else [row]


def _button_datas(buttons) -> list[str]:
    """callback_data кнопок для старой (btn.data) и новой (btn.type.data) схем TL."""
    datas = []
    for row in _button_rows(buttons):
        for btn in row:
            data = getattr(btn, "data", None)
            if data is None:
                data = getattr(getattr(btn, "type", None), "data", None)
            if data is None:
                continue
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            datas.append(data)
    return datas


def _button_texts(buttons) -> list[str]:
    texts = []
    for row in _button_rows(buttons):
        for btn in row:
            t = getattr(btn, "text", None)
            if not isinstance(t, str):
                # Button-обёртка новых telethon: .text — метод.
                t = t() if callable(t) else ""
            texts.append(t)
    return texts


class BotUiTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        self.bot = FakeBot()
        register_handlers(self.bot, self.db, ADMIN_ID)
        register_interests_handlers(self.bot, self.db, ADMIN_ID)
        register_admin_handlers(self.bot, self.db, ADMIN_ID)

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _callback(self, data: str, sender_id: int = ADMIN_ID) -> FakeCallbackEvent:
        event = FakeCallbackEvent(data.encode("utf-8"), sender_id)
        asyncio.run(self.bot.dispatch_callback(event))
        return event

    def _message(self, text: str, pattern: str, sender_id: int = ADMIN_ID) -> FakeMessageEvent:
        handlers = self.bot.handlers_by_pattern(pattern)
        assert handlers, f"хендлер {pattern} не зарегистрирован"
        event = FakeMessageEvent(text, sender_id)
        event.pattern_match = re.match(pattern, text)
        asyncio.run(handlers[0](event))
        return event

    # --- главное меню и пагинация ---
    def test_main_menu_renders(self):
        event = self._callback("main")
        self.assertIn("AirRadar AI — главное меню", event.edits[0]["text"])
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("mysubs", datas)
        self.assertTrue(datas)

    def test_region_pagination(self):
        event = self._callback("rp:0")
        self.assertIn("Выбери область", event.edits[0]["text"])
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("rs:kyivska", datas)
        event2 = self._callback("rp:1")
        self.assertIn("Выбери область", event2.edits[0]["text"])

    # --- «Моя зона»: inline и reply-клавиатуры нельзя смешивать ---
    def test_myzone_no_mixed_keyboards(self):
        """Регрессия «You cannot mix inline with normal buttons»: «Моя зона»
        шлёт ДВА сообщения — inline-меню отдельно, reply-кнопка геолокации
        отдельно. Прод-лог 19:41: смешение роняло кнопку локации."""
        event = self._callback("myzone")
        self.assertGreaterEqual(len(event.respond_buttons), 2)
        kb0, kb1 = event.respond_buttons[0], event.respond_buttons[1]
        texts0, datas0 = _button_texts(kb0), _button_datas(kb0)
        texts1, datas1 = _button_texts(kb1), _button_datas(kb1)
        # Сообщение 1: inline-меню (кнопки с callback_data), без гео-кнопки.
        self.assertIn("main", datas0)
        self.assertNotIn("Надіслати локацію", " ".join(texts0))
        # Сообщение 2: только reply-кнопка геолокации, без inline.
        self.assertEqual(datas1, [])
        self.assertIn("Надіслати локацію", " ".join(texts1))

    def test_zone_kb_has_no_inline_buttons(self):
        """_zone_kb() — только reply-кнопки (геолокация), без inline."""
        from bot_ui import _zone_kb

        kb = _zone_kb()
        self.assertTrue(kb)
        self.assertEqual(_button_datas(kb), [])
        self.assertIn("Надіслати локацію", " ".join(_button_texts(kb)))

    # --- меню Киева с кнопками-берегами ---
    def test_kyiv_region_menu_has_zone_buttons(self):
        event = self._callback("rs:kyivska")
        self.assertIn("Київ та область", event.edits[0]["text"])
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("zsb:kyiv_left", datas)
        self.assertIn("zsb:kyiv_right", datas)
        texts = _button_texts(event.edits[0]["buttons"])
        self.assertIn("🔔 Лівий берег", texts)
        self.assertIn("🔔 Правий берег", texts)

    def test_regular_region_has_single_sub_button(self):
        event = self._callback("rs:odeska")
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("rsb:odeska", datas)
        self.assertNotIn("zsb:kyiv_left", datas)

    # --- toggle подписки на регион ---
    def test_region_sub_toggle(self):
        event = self._callback("rsb:odeska")
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "odeska"))
        self.assertIn("🔔 Подписка на Одеська обл.", [a for a in event.answers if a])
        event2 = self._callback("rsb:odeska")
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "odeska"))
        self.assertIn("🔕 Отписка от Одеська обл.", [a for a in event2.answers if a])

    # --- toggle подписки на берег Киева (проверяем в БД) ---
    def test_zone_sub_toggle_left(self):
        self._callback("zsb:kyiv_left")
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "kyiv_left"))
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "kyiv_right"))
        # Кнопка после toggle показывает ✅.
        event = self._callback("rs:kyivska")
        self.assertIn("✅ Лівий берег", _button_texts(event.edits[0]["buttons"]))
        # Повторное нажатие — отписка.
        self._callback("zsb:kyiv_left")
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "kyiv_left"))

    def test_zone_sub_toggle_right(self):
        self._callback("zsb:kyiv_right")
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "kyiv_right"))
        self._callback("zsb:kyiv_right")
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "kyiv_right"))

    # --- Мои подписки ---
    def test_my_subs_empty(self):
        event = self._callback("mysubs")
        self.assertIn("Пока пусто", event.edits[0]["text"])
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("rp:0", datas)

    def test_my_subs_lists_subscriptions(self):
        self.db.subscribe(ADMIN_ID, "odeska")
        self.db.subscribe(ADMIN_ID, "kyiv_left")
        event = self._callback("mysubs")
        self.assertIn("Одеська обл.", event.edits[0]["text"])
        self.assertIn("Київ — лівий берег", event.edits[0]["text"])
        datas = _button_datas(event.edits[0]["buttons"])
        self.assertIn("rs:odeska", datas)
        self.assertIn("rs:kyiv_left", datas)

    # --- /city: подписка по названию города/района ---
    def test_city_command_subscribes_by_district(self):
        event = self._message("/city Дарниця", r"^/city\s+(.+)$")
        # Район левого берега → подписка на берег, а не на весь Киев.
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "kyiv_left"))
        self.assertIn("🔔 Подписка на Київ — лівий берег", event.responses[0])
        datas = _button_datas(event.buttons[0])
        self.assertIn("rs:kyiv_left", datas)
        self.assertIn("mysubs", datas)

    def test_city_command_toggles_off(self):
        self._message("/city Одеса", r"^/city\s+(.+)$")
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "odeska"))
        event = self._message("/city Одеса", r"^/city\s+(.+)$")
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "odeska"))
        self.assertIn("🔕 Отписка от Одеська обл.", event.responses[0])

    def test_city_command_unknown(self):
        event = self._message("/city блаблабла12345", r"^/city\s+(.+)$")
        self.assertIn("Не нашёл", event.responses[0])
        self.assertEqual(self.db.user_subscriptions(ADMIN_ID), [])

    def test_city_exact_city_wins_over_kyiv_district(self):
        # «Дніпро» — это город, а не дніпровський масив Киева.
        self._message("/city Дніпро", r"^/city\s+(.+)$")
        self.assertTrue(self.db.is_subscribed(ADMIN_ID, "dnipropetrovska"))
        self.assertFalse(self.db.is_subscribed(ADMIN_ID, "kyiv_left"))

    # --- Interests: itt toggle instant → digest → отписка ---
    def test_interests_topic_toggle_cycle(self):
        event = self._callback("itt:crypto")
        self.assertEqual(self.db.topic_delivery_mode(ADMIN_ID, "crypto"), "instant")
        self.assertIn("🔔 Мгновенно: ₿ Крипта", [a for a in event.answers if a])
        event2 = self._callback("itt:crypto")
        self.assertEqual(self.db.topic_delivery_mode(ADMIN_ID, "crypto"), "digest")
        self.assertIn("🕗 Дайджест (утро/вечер): ₿ Крипта", [a for a in event2.answers if a])
        event3 = self._callback("itt:crypto")
        self.assertFalse(self.db.is_subscribed_topic(ADMIN_ID, "crypto"))
        self.assertIn("🔕 Отписка: ₿ Крипта", [a for a in event3.answers if a])

    # --- Interests: /add_channel с валидацией ---
    def test_add_channel_valid_and_normalized(self):
        event = self._message("/add_channel t.me/example_channel", r"^/add_channel(?:\s+(.+))?$")
        self.assertIn("@example_channel", self.db.user_channels(ADMIN_ID))
        self.assertIn("добавлен", event.responses[0])

    def test_add_channel_rejects_garbage(self):
        event = self._message("/add_channel привет как дела!!", r"^/add_channel(?:\s+(.+))?$")
        self.assertEqual(self.db.user_channels(ADMIN_ID), [])
        self.assertIn("не похоже", event.responses[0])

    def test_add_channel_duplicate_is_reported(self):
        self._message("/add_channel @dupchan", r"^/add_channel(?:\s+(.+))?$")
        event = self._message("/add_channel @dupchan", r"^/add_channel(?:\s+(.+))?$")
        self.assertIn("уже добавлен", event.responses[0])
        self.assertEqual(self.db.user_channels(ADMIN_ID).count("@dupchan"), 1)

    def test_del_channel(self):
        self._message("/add_channel @gonechan", r"^/add_channel(?:\s+(.+))?$")
        event = self._message("/del_channel @gonechan", r"^/del_channel\s+(\S+)")
        self.assertNotIn("@gonechan", self.db.user_channels(ADMIN_ID))
        self.assertIn("удалён", event.responses[0])

    def test_del_channel_missing(self):
        event = self._message("/del_channel @nothere", r"^/del_channel\s+(\S+)")
        self.assertIn("нет в твоём списке", event.responses[0])

    # --- админка: /ban_channel → db.disable_channel ---
    def test_admin_ban_channel(self):
        pattern = r"^/ban_channel\s+(\S+)"
        handlers = self.bot.handlers_by_pattern(pattern)
        self.assertTrue(handlers)
        self.db.channel_seen("@badchannel", "military")
        event = FakeMessageEvent("/ban_channel @badchannel", ADMIN_ID)
        event.pattern_match = re.match(pattern, event.text)
        asyncio.run(handlers[0](event))
        self.assertTrue(self.db.is_channel_disabled("@badchannel", "military"))

    def test_non_admin_cannot_ban_channel(self):
        pattern = r"^/ban_channel\s+(\S+)"
        handler = self.bot.handlers_by_pattern(pattern)[0]
        self.db.channel_seen("@badchannel", "military")
        event = FakeMessageEvent("/ban_channel @badchannel", sender_id=999)
        event.pattern_match = re.match(pattern, event.text)
        asyncio.run(handler(event))
        self.assertFalse(self.db.is_channel_disabled("@badchannel", "military"))


if __name__ == "__main__":
    unittest.main()
