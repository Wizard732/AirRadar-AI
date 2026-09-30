"""bot_ui.py — интерактивное меню бота (InlineKeyboard) в личке @AirRadar_AI_bot.

Структура меню:
  /start  →  Главное меню
                [🏙 Выбрать область]   [📊 Общая статистика]   [⏱ Текущие угрозы]

  «Выбрать область» → список 24 областей (пагинация по 8) → выбор области
                        → Меню региона:
                            [📊 Статистика тревог]  [⏱ ETA угроз]
                            [💥 История ударов]     [🔥 Последствия]
                            [← К списку областей]

Все кнопки используют callback_data с префиксами для маршрутизации в
register_handlers(). Текст сообщений — HTML (parse_mode=HTML).
"""

from __future__ import annotations

import html
import logging
import time

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

from database import Database
from eta import build_eta_text
from regions import (
    REGIONS,
    ZONE_NAMES,
    all_region_slugs,
    find_regions_by_text,
    region_name,
)


def _webapp_button(text: str, url: str):
    """Кнопка-WebApp, совместимая с telethon 1.36–1.44 и 1.45+.

    В 1.45 схема TL переехала на единый KeyboardInlineButton с
    InlineButtonTypeWebView, а старый KeyboardButtonWebView исчез.
    """
    try:
        from telethon.tl.types import KeyboardButtonWebView  # telethon < 1.45
        return KeyboardButtonWebView(text=text, url=url)
    except ImportError:
        from telethon.tl.types import InlineButtonTypeWebView, KeyboardInlineButton
        return KeyboardInlineButton(text, InlineButtonTypeWebView(url))

logger = logging.getLogger(__name__)

# Кол-во кнопок-областей в одной «странице» пагинации.
PAGE_SIZE = 8

# Префиксы callback_data, чтобы различать действия.
CB_REGION_PAGE = "rp:"   # страница списка областей: rp:0, rp:1, ...
CB_REGION_SELECT = "rs:" # выбор конкретного региона: rs:kyivska
CB_REGION_STATS = "rst:" # статистика тревог региона: rst:kyivska
CB_REGION_ETA = "ret:"   # ETA региона: ret:kyivska
CB_REGION_HIST = "rhi:"  # история ударов региона: rhi:kyivska
CB_REGION_CONS = "rco:"  # последствия региона: rco:kyivska
CB_REGION_SUB = "rsb:"   # подписка на регион: rsb:kyivska
CB_ZONE_SUB = "zsb:"     # подписка на берег Киева: zsb:kyiv_left / zsb:kyiv_right
CB_MY_SUBS = "mysubs"    # мои подписки (регионы + берега Киева)
CB_MAIN = "main"         # главное меню
CB_STATS_ALL = "sall"    # общая статистика
CB_ACTIVE = "active"     # текущие угрозы
CB_NIGHT_MODE = "night"  # персональный ночной режим (toggle)
CB_REPORT = "report"     # подсказка: сообщить угрозу своей геопозицией
CB_WEAPONS = "weapons"   # меню персонального фильтра типов угроз
CB_WEAPONS_ALL = "wall"  # «увімкнути всі»: сброс фильтра типов
CB_WEAPON_TOGGLE = "wt:" # toggle группы типов: wt:ballistic / wt:uav / wt:other

# Человекочитаемые подписи типов угроз.
TYPE_LABELS = {
    "missile": "🚀 Ракеты",
    "ballistic": "🚀 Балістика",
    "cruise_missile": "🚀 Крилаті ракети",
    "kab": "✈️ КАБ",
    "aviation": "✈️ Авіація",
    "uav": "🛸 БПЛА",
    "shahed": "🛸 Shahed",
    "fpv": "🛸 ФПВ-дрони",
    "recon_drone": "👁 Розвідка",
    "mlrs": "🔴 РСЗО",
    "artillery": "🔴 Артиллерия",
    "explosion": "💥 Взрывы",
    "air_defense": "🛡 ППО",
    "alert": "🟡 Тривога",
    "stand_down": "🟢 Отбои",
    "other": "🚨 Прочее",
}

# Группы персонального фильтра «Типи тривог» (совпадают с weapon_group в main).
WEAPON_FILTERS = [
    ("ballistic", "🚀 Ракети / балістика"),
    ("uav", "🛸 БпЛА (Shahed/FPV)"),
    ("other", "💥 Артилерія, приліт, ППО"),
]


# =====================================================================
#  Сборка клавиатур
# =====================================================================

CB_INTERESTS = "interests"  # переход в Interests-модуль


def _main_menu_kb():
    return [
        [Button.inline("🪖 Военные алерты", data=CB_REGION_PAGE + "0")],
        [Button.inline("📍 Повідомити загрозу", data=CB_REPORT)],
        [Button.inline("🔔 Мої підписки", data=CB_MY_SUBS)],
        [Button.inline("🎯 Типи тривог", data=CB_WEAPONS)],
        [Button.inline("📰 Новости по интересам", data=CB_INTERESTS)],
        [
            Button.inline("📊 Общая статистика", data=CB_STATS_ALL),
            Button.inline("📋 Детальна статистика", data="stats_detail"),
        ],
        [Button.inline("⏱ Текущие угрозы", data=CB_ACTIVE)],
    ]


def _main_menu_with_webapp(webapp_url: str, map_webapp_url: str = ""):
    """Главное меню + кнопка-WebApp (Mini App открывается по URL).

    Рядом с WebApp-кнопкой добавляется обычная URL-кнопка: карта всегда
    открывается и в браузере, даже если Telegram-клиент не поддерживает
    WebApp-кнопки или домен не привязан к боту.
    """
    rows = []
    if map_webapp_url:
        rows.append([_webapp_button("🗺 Live threat map", map_webapp_url)])
        rows.append([Button.url("🌐 Відкрити карту в браузері", map_webapp_url)])
    if webapp_url:
        rows.append([_webapp_button("⚙️ Settings (Mini App)", webapp_url)])
    return rows + [
        [Button.inline("🪖 Военные алерты", data=CB_REGION_PAGE + "0")],
        [Button.inline("📍 Повідомити загрозу", data=CB_REPORT)],
        [Button.inline("🔔 Мої підписки", data=CB_MY_SUBS)],
        [Button.inline("🎯 Типи тривог", data=CB_WEAPONS)],
        [Button.inline("📰 Новости по интересам", data=CB_INTERESTS)],
        [
            Button.inline("📊 Общая статистика", data=CB_STATS_ALL),
            Button.inline("⏱ Текущие угрозы", data=CB_ACTIVE),
        ],
    ]


def _regions_kb(page: int):
    """Клавиатура выбора области с пагинацией."""
    slugs = all_region_slugs()
    start = page * PAGE_SIZE
    chunk = slugs[start : start + PAGE_SIZE]

    # По 2 кнопки в ряд для читаемости названий.
    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for slug in chunk[i : i + 2]:
            row.append(Button.inline(region_name(slug), data=CB_REGION_SELECT + slug))
        rows.append(row)

    # Кнопки навигации по страницам.
    total_pages = (len(slugs) + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ Назад", data=f"{CB_REGION_PAGE}{page - 1}"))
    nav.append(Button.inline("🏠 Главное меню", data=CB_MAIN))
    if page + 1 < total_pages:
        nav.append(Button.inline("Вперёд ▶️", data=f"{CB_REGION_PAGE}{page + 1}"))
    rows.append(nav)

    return rows


def _region_menu_kb(slug: str, subscribed: bool = False, zones: tuple[bool, bool] | None = None):
    """Меню конкретного региона. Кнопка подписки меняется в зависимости от статуса.

    Для Киева (kyivska) вместо одной кнопки — две кнопки-берега
    (лівий/правий) с toggle и ✅ если подписан. zones = (лівий, правий).
    """
    if slug == "kyivska":
        left, right = zones or (False, False)
        sub_row = [
            Button.inline(
                ("✅ " if left else "🔔 ") + "Лівий берег",
                data=CB_ZONE_SUB + "kyiv_left",
            ),
            Button.inline(
                ("✅ " if right else "🔔 ") + "Правий берег",
                data=CB_ZONE_SUB + "kyiv_right",
            ),
        ]
    else:
        sub_row = [Button.inline(
            "🔕 Отписаться" if subscribed else "🔔 Подписаться",
            data=CB_REGION_SUB + slug,
        )]
    return [
        [
            Button.inline("📊 Статистика тревог", data=CB_REGION_STATS + slug),
            Button.inline("⏱ ETA угроз", data=CB_REGION_ETA + slug),
        ],
        [
            Button.inline("💥 История ударов", data=CB_REGION_HIST + slug),
            Button.inline("🔥 Последствия", data=CB_REGION_CONS + slug),
        ],
        sub_row,
        [Button.inline("◀️ К списку областей", data=CB_REGION_PAGE + "0")],
        [Button.inline("🏠 Главное меню", data=CB_MAIN)],
    ]


# =====================================================================
#  Тексты сообщений
# =====================================================================

def _main_text() -> str:
    return (
        "📍 <b>AirRadar AI — главное меню</b>\n\n"
        "Выбери раздел кнопками ниже. Здесь доступна статистика угроз, "
        "ETA (время прилёта) и история ударов по областям Украины.\n\n"
        "💡 Чтобы получать алерты в ЛС: <b>Военные алерты</b> → область → "
        "🔔 Подписаться. Или команда <code>/city &lt;город&gt;</code>.\n"
        "📍 Видишь угрозу рядом — <b>Повідомити загрозу</b> или <code>/report</code>: "
        "твоя точка появится на живой карте.\n"
        "🌙 Нічний режим: 23:00–06:00 в ЛС лише критичні (ракети/балістика/КАБ)."
    )


def _my_subs_text(subs: list[str]) -> str:
    if not subs:
        return (
            "🔔 <b>Мої підписки</b>\n\n"
            "Пока пусто. Открой <b>Военные алерты</b> → выбери область → "
            "🔔 Подписаться, и алерты этого региона будут приходить в ЛС.\n\n"
            "Для Киева подписка — по берегам: <code>/city Дарниця</code>."
        )
    lines = ["🔔 <b>Мої підписки</b>\n", "Алерты этих регионов приходят в ЛС:\n"]
    for slug in subs:
        lines.append(f"• {region_name(slug)}")
    lines.append("\nНажми на регион, чтобы открыть его меню (подписка/отписка).")
    return "\n".join(lines)


def _my_subs_kb(subs: list[str]) -> list:
    """Кнопки подписанных регионов (2 в ряд) + возврат в меню."""
    rows = []
    for i in range(0, len(subs), 2):
        rows.append([
            Button.inline(region_name(slug), data=CB_REGION_SELECT + slug)
            for slug in subs[i : i + 2]
        ])
    rows.append([Button.inline("➕ Добавить область", data=CB_REGION_PAGE + "0")])
    rows.append([Button.inline("🏠 Главное меню", data=CB_MAIN)])
    return rows


def _weapons_text(db: Database, user_id: int) -> str:
    """Экран «Типи тривог»: какие группы алертов доставлять в ЛС."""
    allowed = db.get_weapon_classes(user_id)
    if allowed:
        parts = [p.strip() for p in allowed.split(",") if p.strip()]
        enabled = " · ".join(
            label for key, label in WEAPON_FILTERS if key in parts
        ) or "—"
    else:
        enabled = "усі типи (фільтр вимкнено)"
    return (
        "🎯 <b>Типи тривог</b>\n\n"
        f"Зараз отримуєте: <b>{html.escape(enabled)}</b>\n\n"
        "Натисніть на тип, щоб увімкнути/вимкнути його. "
        "🟢 <b>Відбій надходить завжди</b> — це управляюче повідомлення, "
        "воно фільтром не глушиться.\n"
        "💡 Корисно, якщо укриття далеко: лишіть лише балістику."
    )


def _weapons_kb(db: Database, user_id: int) -> list:
    """Кнопки toggle групп типов + сброс «увімкнути всі» + назад в меню."""
    allowed = {
        p.strip() for p in db.get_weapon_classes(user_id).split(",") if p.strip()
    }
    rows = []
    for key, label in WEAPON_FILTERS:
        # Пустой фильтр = все группы включены.
        on = not allowed or key in allowed
        rows.append([Button.inline(
            ("✅ " if on else "🔕 ") + label,
            data=CB_WEAPON_TOGGLE + key,
        )])
    rows.append([Button.inline("🔔 Увімкнути всі", data=CB_WEAPONS_ALL)])
    rows.append([Button.inline("🏠 Главное меню", data=CB_MAIN)])
    return rows


def _region_menu_text(slug: str) -> str:
    if slug == "kyivska":
        return (
            f"📍 <b>{region_name(slug)}</b>\n\n"
            "Выбери, что показать. Подписка — по берегам Днепра "
            "(Лівий/Правий), алерты Киева приходят в ЛС по выбранному берегу."
        )
    return f"📍 <b>{region_name(slug)}</b>\n\nВыбери, что показать:"


def _stats_all_text(db: Database) -> str:
    """Сводка по всем регионам за разные периоды."""
    now = int(time.time())
    day = now - 86400
    week = now - 7 * 86400

    def render(since: float, label: str) -> str:
        counts = db.confirmed_incident_counts(region=None, since=int(since))
        total = sum(counts.values())
        parts = [f"<b>{label}</b> (всего {total})"]
        for t, c in sorted(counts.items(), key=lambda x: -x[1]):
            parts.append(f"  {TYPE_LABELS.get(t, t)}: {c}")
        return "\n".join(parts) if total else f"<b>{label}</b>: підтверджених інцидентів не зафіксовано"

    active = db.active_threats(within_seconds=1800)
    active_regions = ", ".join(region_name(r["region"]) for r in active[:10]) or "нет"

    # Открытая метрика точности: опережение официальной сирены за неделю.
    lead = db.siren_lead_stats(days=7)
    lead_line = ""
    if lead["episodes"]:
        lead_min = lead["avg_lead_sec"] / 60
        lead_line = (
            f"\n\n⚡ <b>Випередження сирени</b> (7 днів): "
            f"{lead['before_count']} з {lead['episodes']} епізодів раніше офіційної тривоги"
            + (f", у середньому на {lead_min:.0f} хв" if lead_min > 0 else "")
        )

    return (
        "📊 <b>Подтверждённые инциденты</b>\n\n"
        f"{render(day, 'За 24 часа')}\n\n"
        f"{render(week, 'За неделю')}\n\n"
        f"🔴 <b>Активные угрозы (30 мин):</b> {len(active)}\n"
        f"Регионы: {active_regions}"
        f"{lead_line}"
    )


def _active_text(db: Database) -> str:
    threats = db.active_threats(within_seconds=1800)
    if not threats:
        return "⏱ <b>Подтверждённые активные сообщения</b>\n\nЗа последние 30 минут в источниках бота нет подтверждённых активных сообщений."
    lines = ["⏱ <b>Подтверждённые активные сообщения</b> (за 30 мин)\n"]
    for t in threats[:15]:
        ago = int((time.time() - t["ts"]) / 60)
        lines.append(
            f"{TYPE_LABELS.get(t['type'], '🚨')} {region_name(t['region'])} "
            f"— {ago} мин назад\n   <i>{html.escape(t['text'][:60])}</i>"
        )
    return "\n".join(lines)


def _region_stats_text(db: Database, slug: str) -> str:
    now = int(time.time())
    day = now - 86400
    week = now - 7 * 86400
    name = region_name(slug)

    def render(since: float, label: str) -> str:
        counts = db.confirmed_incident_counts(region=slug, since=int(since))
        total = sum(counts.values())
        if not total:
            return f"<b>{label}</b>: підтверджених інцидентів не зафіксовано"
        parts = [f"<b>{label}</b> (всего {total})"]
        for t, c in sorted(counts.items(), key=lambda x: -x[1]):
            parts.append(f"  {TYPE_LABELS.get(t, t)}: {c}")
        return "\n".join(parts)

    # Legacy alert intervals may come from old raw posts, so do not present
    # their duration as a reliable prediction or operational fact.
    avg_line = "Длительность тревог: показывается только по официальным данным"

    db.expire_stale_alerts()
    is_active = slug in db.active_alert_regions()
    status = "🔴 Подтверждённая тревога активна" if is_active else "⚪ Нет активного статуса от источников бота"

    return (
        f"📊 <b>{name}</b> — статистика\n\n"
        f"{render(day, 'За 24 часа')}\n\n"
        f"{render(week, 'За неделю')}\n\n"
        f"{avg_line}\n"
        f"Статус: {status}\n"
        "Показано лише інциденти, підтверджені незалежними або офіційними джерелами."
    )


def _region_hist_text(db: Database, slug: str) -> str:
    name = region_name(slug)
    items = db.recent_threats(slug, limit=5)
    if not items:
        return f"🗂 <b>{name}</b> — последние сообщения\n\nЗаписей пока нет."
    lines = [f"🗂 <b>{name}</b> — последние сообщения\n"]
    for it in items:
        when = time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
        lines.append(f"{TYPE_LABELS.get(it['type'], '🚨')} {when}\n   <i>{html.escape(it['text'][:70])}</i>")
    return "\n".join(lines)


def _region_cons_text(db: Database, slug: str) -> str:
    """Show only corroborated/official impact evidence from the last day."""
    name = region_name(slug)
    items = db.confirmed_consequences(slug, since=int(time.time()) - 86400)
    if not items:
        return (
            f"🔥 <b>{name}</b> — підтверджені наслідки\n\n"
            "Немає підтверджених повідомлень про наслідки за останні 24 години."
        )
    lines = [f"🔥 <b>{name}</b> — підтверджені наслідки (24 год)\n"]
    for it in items:
        when = time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
        lines.append(
            f"💥 {when} · {it['source_count']} незалежних джерел\n"
            f"   <i>{html.escape((it['text'] or '')[:160])}</i>"
        )
    return "\n".join(lines)


def _region_eta_text(db: Database, slug: str) -> str:
    # Каскад: пары threats → события threat_events → риск прилёта → справка.
    # В конце — статистика волн региона («коли відбій / наступна хвиля»):
    # ошибки прогноза не должны ломать ETA-текст.
    text = build_eta_text(db, slug, region_name(slug))
    try:
        from wave_forecast import format_wave_forecast
        waves = format_wave_forecast(db, slug, "")
        if waves:
            body = waves.split("\n", 1)[-1].strip()  # без своего заголовка
            text += "\n\n" + body
    except Exception:  # noqa: BLE001 — прогноз не роняет ETA
        pass
    return text


def _city_results_kb(matches: list[str]) -> list:
    """Кнопки результатов /city: выбор региона + мои подписки."""
    rows = []
    for i in range(0, len(matches[:6]), 2):
        rows.append([
            Button.inline(region_name(slug), data=CB_REGION_SELECT + slug)
            for slug in matches[i : i + 2]
        ])
    rows.append([Button.inline("🔔 Мої підписки", data=CB_MY_SUBS)])
    rows.append([Button.inline("🏠 Главное меню", data=CB_MAIN)])
    return rows


# =====================================================================
#  Регистрация обработчиков
# =====================================================================

def register_handlers(
    bot: TelegramClient, db: Database, admin_id: int, webapp_url: str = "", map_webapp_url: str = ""
) -> None:
    """Навесить на bot-клиента обработчики /start и нажатий кнопок.

    admin_id: Telegram user id, которому разрешено меню.
    webapp_url: если задан — в меню показывается кнопка Mini App.
    """
    if getattr(bot, "_airradar_ui_handlers_registered", False):
        logger.warning("UI-обработчики уже зарегистрированы; повторная регистрация пропущена")
        return
    bot._airradar_ui_handlers_registered = True

    def _is_admin(user_id: int) -> bool:
        return db.is_admin(user_id, admin_id)

    def _menu_kb(user_id: int):
        rows = _main_menu_with_webapp(webapp_url, map_webapp_url) if (webapp_url or map_webapp_url) else _main_menu_kb()
        # Персональный ночной режим: 23:00–06:00 в ЛС только критичные классы.
        state = "увімкнено ✅" if db.get_night_mode(user_id) else "вимкнено"
        return rows + [[Button.inline(f"🌙 Нічний режим: {state}", data=CB_NIGHT_MODE)]]

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start"))
    async def _start(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        # Меню доступно всем пользователям (подписки, статистика, алерты).
        await event.respond(_main_text(), parse_mode="html", buttons=_menu_kb(event.sender_id))

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/city\s+(.+)$"))
    async def _city(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        """Подписка на регион/берег по названию города: /city Дарниця.

        Находит подходящие регионы/зоны и сразу подписывает (toggle —
        повторная команда отписывает). Ответ — кнопки, чтобы можно было
        открыть меню региона или отписаться.
        """
        query = (event.pattern_match.group(1) or "").strip()
        matches = find_regions_by_text(query)
        if not matches:
            await event.respond(
                f"🔎 Не нашёл «{html.escape(query)}». Попробуй область или район, "
                "например: <code>/city Дніпро</code>, <code>/city Дарниця</code>.",
                parse_mode="html",
            )
            return
        # Подписываем на первый (самый точный) результат, остальные — кнопками.
        slug = matches[0]
        if db.is_subscribed(event.sender_id, slug):
            db.unsubscribe(event.sender_id, slug)
            answer = "🔕 Отписка от " + region_name(slug)
        else:
            db.subscribe(event.sender_id, slug)
            answer = "🔔 Подписка на " + region_name(slug)
        await event.respond(answer, parse_mode="html", buttons=_city_results_kb(matches))

    @bot.on(events.CallbackQuery())
    async def _callback(event) -> None:  # noqa: ANN001
        # Кнопки меню доступны всем. Админ-функции проверяются отдельно.

        data = event.data.decode("utf-8") if isinstance(event.data, bytes) else event.data

        async def _safe_edit(text: str, buttons) -> None:
            """Обёртка над event.edit, глотающая MessageNotModifiedError.

            Возникает, когда пользователь нажимает кнопку, но текст не изменился
            (например, нажал «Статистика» дважды). Это не ошибка — просто noop.
            """
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
            except MessageNotModifiedError:
                pass  # контент не изменился — это нормально, не падаем
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось обновить сообщение меню: %s", exc)

        try:
            # Маршрутизация по префиксу callback_data.
            if data == CB_MAIN:
                await _safe_edit(_main_text(), _menu_kb(event.sender_id))

            elif data == CB_NIGHT_MODE:
                # Toggle персонального ночного режима + перерисовка меню.
                new_state = not db.get_night_mode(event.sender_id)
                db.set_night_mode(event.sender_id, new_state)
                await event.answer(
                    "🌙 Нічний режим увімкнено: 23:00–06:00 лише критичні"
                    if new_state
                    else "🌙 Нічний режим вимкнено"
                )
                await _safe_edit(_main_text(), _menu_kb(event.sender_id))

            elif data == CB_REPORT:
                # Подсказка: отправь геопозицию — точка появится на карте.
                from geo_report import REPORT_HINT
                await event.answer()
                await event.respond(REPORT_HINT, parse_mode="html")

            elif data == CB_WEAPONS:
                # Экран «Типи тривог»: персональный фильтр групп алертов.
                await _safe_edit(
                    _weapons_text(db, event.sender_id),
                    _weapons_kb(db, event.sender_id),
                )

            elif data == CB_WEAPONS_ALL:
                # Сброс фильтра: пустой CSV = приходят все типы.
                db.set_weapon_classes(event.sender_id, "")
                await event.answer("🔔 Усі типи тривог увімкнено")
                await _safe_edit(
                    _weapons_text(db, event.sender_id),
                    _weapons_kb(db, event.sender_id),
                )

            elif data.startswith(CB_WEAPON_TOGGLE):
                # Toggle группы. Пустой CSV означает «все включены», поэтому
                # при снятии последней галочки показываем подсказку, а не
                # молча включаем всё обратно — иначе кнопка врёт.
                key = data[len(CB_WEAPON_TOGGLE):]
                if key in {"ballistic", "uav", "other"}:
                    allowed = {
                        p.strip()
                        for p in db.get_weapon_classes(event.sender_id).split(",")
                        if p.strip()
                    }
                    if not allowed:
                        allowed = {"ballistic", "uav", "other"}
                    if key in allowed:
                        if len(allowed) == 1:
                            await event.answer(
                                "Має залишитися хоча б один тип (відбій надходить завжди)",
                                alert=True,
                            )
                        else:
                            allowed.discard(key)
                            db.set_weapon_classes(event.sender_id, ",".join(sorted(allowed)))
                            await event.answer("🔕 Тип вимкнено")
                    else:
                        allowed.add(key)
                        db.set_weapon_classes(event.sender_id, ",".join(sorted(allowed)))
                        await event.answer("✅ Тип увімкнено")
                await _safe_edit(
                    _weapons_text(db, event.sender_id),
                    _weapons_kb(db, event.sender_id),
                )

            elif data.startswith("fbu:") or data.startswith("fbn:"):
                # Фидбек под алертом в ЛС: «✅ корисно / ➖ шум».
                vote = "useful" if data.startswith("fbu:") else "noise"
                key = data[4:].strip()
                if key:
                    db.add_alert_feedback(key, vote)
                await event.answer(
                    "Дякуємо за відгук!" if vote == "useful"
                    else "Прийнято — працюємо над точністю."
                )

            elif data == CB_STATS_ALL:
                await _safe_edit(_stats_all_text(db), _menu_kb(event.sender_id))

            elif data == "stats_detail":
                # Детальная статистика за неделю (все регионы).
                from stats import format_detailed_stats
                text = format_detailed_stats(db, days=7)
                await _safe_edit(text, _menu_kb(event.sender_id))

            elif data == CB_MY_SUBS:
                # Мои подписки: регионы + берега Киева одним списком.
                subs = db.user_subscriptions(event.sender_id)
                await _safe_edit(_my_subs_text(subs), _my_subs_kb(subs))

            elif data == CB_ACTIVE:
                await _safe_edit(_active_text(db), _menu_kb(event.sender_id))

            elif data == CB_INTERESTS:
                # Переход в меню Interests-модуля (его кнопки определяет interests_ui).
                from interests_ui import _interests_main_kb, _interests_main_text
                await _safe_edit(_interests_main_text(), _interests_main_kb())

            elif data.startswith(CB_REGION_PAGE):
                page = int(data[len(CB_REGION_PAGE):] or "0")
                await _safe_edit(
                    "🏙 <b>Выбери область</b>\n\nЛистай кнопками ◀️ ▶️.",
                    _regions_kb(page),
                )

            elif data.startswith(CB_REGION_SELECT):
                slug = data[len(CB_REGION_SELECT):]
                await event.answer()
                if slug in REGIONS:
                    sub = db.is_subscribed(event.sender_id, slug)
                    zones = None
                    if slug == "kyivska":
                        zones = (
                            db.is_subscribed(event.sender_id, "kyiv_left"),
                            db.is_subscribed(event.sender_id, "kyiv_right"),
                        )
                    await _safe_edit(_region_menu_text(slug), _region_menu_kb(slug, sub, zones))

            elif data.startswith(CB_REGION_STATS):
                slug = data[len(CB_REGION_STATS):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_region_stats_text(db, slug), _region_menu_kb(slug, sub, zones))

            elif data.startswith(CB_REGION_ETA):
                slug = data[len(CB_REGION_ETA):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_region_eta_text(db, slug), _region_menu_kb(slug, sub, zones))

            elif data.startswith(CB_REGION_HIST):
                slug = data[len(CB_REGION_HIST):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_region_hist_text(db, slug), _region_menu_kb(slug, sub, zones))

            elif data.startswith(CB_REGION_CONS):
                slug = data[len(CB_REGION_CONS):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_region_cons_text(db, slug), _region_menu_kb(slug, sub, zones))

            elif data.startswith(CB_ZONE_SUB):
                # Toggle подписки на берег Киева (kyiv_left / kyiv_right).
                zone = data[len(CB_ZONE_SUB):]
                if zone in ZONE_NAMES:
                    if db.is_subscribed(event.sender_id, zone):
                        db.unsubscribe(event.sender_id, zone)
                        await event.answer("🔕 Отписка от «" + region_name(zone) + "»")
                    else:
                        db.subscribe(event.sender_id, zone)
                        await event.answer("🔔 Подписка на «" + region_name(zone) + "»")
                zones = (
                    db.is_subscribed(event.sender_id, "kyiv_left"),
                    db.is_subscribed(event.sender_id, "kyiv_right"),
                )
                await _safe_edit(_region_menu_text("kyivska"), _region_menu_kb("kyivska", False, zones))

            elif data.startswith(CB_REGION_SUB):
                slug = data[len(CB_REGION_SUB):]
                # Переключаем подписку.
                if db.is_subscribed(event.sender_id, slug):
                    db.unsubscribe(event.sender_id, slug)
                    await event.answer("🔕 Отписка от " + region_name(slug))
                else:
                    db.subscribe(event.sender_id, slug)
                    await event.answer("🔔 Подписка на " + region_name(slug))
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_region_menu_text(slug), _region_menu_kb(slug, sub, zones))

        except Exception as exc:  # noqa: BLE001 — не роняем меню на ошибке рендера
            logger.exception("Ошибка обработки callback %s: %s", data, exc)
            await event.answer("Ошибка отображения, смотри логи.", alert=True)
