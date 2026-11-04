"""Клавиатуры, тексты и константы меню бота (вынесено из bot_ui.py без изменения логики)."""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError

from kyiv_time import fmt as kyiv_fmt

from database import Database
import menu_screens as ms
from city_coords import region_for_point
from eta import build_eta_text
from fast_filter import public_text
from regions import (
    REGIONS,
    ZONE_NAMES,
    all_region_slugs,
    find_regions_by_text,
    region_name,
)


_webapp_button = ms.webapp_button

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
CB_MY_ZONE = "myzone"    # «Моя зона»: следующая геопозиция = домашний регион
CB_SHELTER = "shelter"   # «Укриття»: экран режима (только тревога + відбій)
CB_SHELTER_TOGGLE = "sht"  # toggle режима «Укриття»
CB_WEAPONS = "weapons"   # меню персонального фильтра типов угроз
CB_WEAPONS_ALL = "wall"  # «увімкнути всі»: сброс фильтра типов
CB_WEAPON_TOGGLE = "wt:" # toggle группы типов: wt:ballistic / wt:uav / wt:other
CB_REGION_WEEK = "rwk:"  # недельный график тревог региона: rwk:kyivska

# TTL ожидания геопозиции после «🎯 Моя зона» (сек): за это время нужно
# прислать локацию, иначе запрос сбрасывается и geo работает как репорт.
ZONE_PROMPT_TTL = 600

# Юникод-бары для «📈 Тиждень» (от минимума к максимуму).
WEEK_BARS = "▁▂▃▄▅▆▇█"

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


# Подписи постоянных reply-кнопок (бабушка-режим): всегда под клавиатурой.
# Хендлер _any_text_menu распознаёт эти строки и выполняет действие сразу.
RB_REGIONS = "🏙 Області"
RB_ACTIVE = "🚨 Зараз"
RB_STATS = "📊 Статистика"
RB_ZONE = "🎯 Моя зона"
RB_SHELTER = "🛡 Укриття"
RB_SUBS = "🔔 Підписки"
RB_HELP = "🏠 Меню"
RB_MAP = "🗺 Карта"
RB_HIST = "💥 Історія"
RB_IMP = "🔥 Наслідки"
RB_GUARD = "🛡 Сторож"
RB_TRACKS = "🛰 Цілі"
RB_PLACES = "📍 Місця"
RB_SHEL = "🏚 Укриття поруч"
RB_FAMILY = "👪 Родина"
RB_SAFE = "✅ Я в укритті"
RB_APP = "📱 Додаток"
# Старі підписи (клавіатура у вже відкритих чатах) працюють, доки користувач не натисне /start.
RB_LEGACY = {
    "🗺 Области": RB_REGIONS, "🚨 Текущие угрозы": RB_ACTIVE, "🔔 Мои подписки": RB_SUBS, "❓ Как пользоваться": RB_HELP,
}


def _persistent_kb() -> list:
    """Постійна reply-клавіатура: великі кнопки, завжди на екрані, вводити текст не потрібно."""
    t = lambda label: Button.text(label, resize=True)
    return [
        [t(RB_MAP), t(RB_TRACKS), t(RB_ACTIVE)],
        [t(RB_ZONE), t(RB_SUBS), t(RB_REGIONS)],
        [t(RB_HELP)],
    ]


def _main_menu_kb():
    return [
        [Button.inline("🪖 Военные алерты", data=CB_REGION_PAGE + "0")],
        [
            Button.inline("🎯 Моя зона", data=CB_MY_ZONE),
            Button.inline("🛡 Укриття", data=CB_SHELTER),
        ],
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
    """Головне меню: усе керується кнопками. WebApp-кнопки (карта, Mini App) — якщо задано адресу сайту."""
    rows = []
    base = ms.site_base(map_webapp_url)
    if base:
        rows.append([_webapp_button("🗺 Карта", base + "/"), _webapp_button("📱 Mini App", ms.app_url(map_webapp_url))])
    rows += [
        [Button.inline("🚨 Зараз", data=CB_ACTIVE), Button.inline("🛰 Цілі на карті", data=ms.CB_TRACKS)],
        [Button.inline("🎯 Моя зона", data=CB_MY_ZONE), Button.inline("🛡 Гео-сторож", data=ms.CB_GUARD)],
        [Button.inline("🔔 Мої підписки", data=CB_MY_SUBS), Button.inline("🏙 Області", data=CB_REGION_PAGE + "0")],
        [Button.inline("🔮 Прогноз для моєї зони", data="fcst"), Button.inline("⏳ Коли відбій / тривога", data="wave")],
        [Button.inline("📍 Повідомити загрозу", data=CB_REPORT)],
    ]
    if webapp_url:
        rows.append([_webapp_button("⚙️ Налаштування (Mini App)", webapp_url)])
    return rows


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
            Button.inline("📈 Тиждень", data=CB_REGION_WEEK + slug),
        ],
        [
            Button.inline("⏱ ETA угроз", data=CB_REGION_ETA + slug),
            Button.inline("💥 История ударов", data=CB_REGION_HIST + slug),
        ],
        [
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
        "📍 <b>AirRadar AI</b>\n\n"
        "Усе керується кнопками. <b>🗺 Карта</b> — цілі в русі з курсом, швидкістю і прогнозом; "
        "<b>📱 Mini App</b> — карта, статистика, історія та наслідки в одному вікні.\n\n"
        "🔔 Тривоги в ЛС: <b>Області</b> → область → «Підписатися» (або <code>/city місто</code>).\n"
        "🛡 <b>Гео-сторож</b> попередить, коли ціль наближається саме до тебе.\n"
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


def _zone_text(db: Database, user_id: int) -> str:
    """Экран «🎯 Моя зона»: текущая домашняя зона по геопозиции."""
    home = db.get_home_region(user_id)
    if home:
        return (
            "🎯 <b>Моя зона</b>\n\n"
            f"Зараз: <b>{html.escape(region_name(home))}</b>.\n\n"
            "Алерты, що торкаються твоєї зони, отримують позначку "
            "«🎯 Торкнеться вашої зони» — видно одразу, навіть без читання.\n\n"
            "Щоб змінити зону — натисни кнопку нижче та надішли нову "
            "геопозицію (скріпка 📎 → «Локація»)."
        )
    return (
        "🎯 <b>Моя зона</b>\n\n"
        "Ще не задана. Натисни кнопку нижче та надішли геопозицію "
        "(скріпка 📎 → «Локація» → «Надіслати мою поточну локацію») — "
        "бот визначить область і позначатиме алерти, що її торкаються.\n\n"
        "⚠️ Координати зберігаються лише як область: точні координати "
        "ніде не публікуються."
    )


def _geo_button(text: str):
    """Reply-кнопка запроса геолокации, совместимая с версиями telethon.

    В новых сборках (unified TL, напр. 1.45+) Button.request_location
    возвращает Button-обёртку, а TL-тип конструируется как
    KeyboardButton(text, ButtonTypeRequestGeoLocation()); в старых —
    KeyboardButtonRequestLocation(text). Возвращаем готовый TL-объект,
    иначе клиент может не сериализовать клавиатуру (кнопка молча пропадёт).
    """
    from telethon import types as tl_types

    try:
        return tl_types.KeyboardButton(text, tl_types.ButtonTypeRequestGeoLocation())
    except (AttributeError, TypeError):
        pass
    try:
        return tl_types.KeyboardButtonRequestLocation(text)
    except (AttributeError, TypeError):
        pass
    try:
        btn = Button.request_location(text)
        # Старый/новый helper мог вернуть класс-обёртку вместо инстанса.
        return btn if not isinstance(btn, type) else Button.text(text)
    except AttributeError:
        return Button.text(text)


def _zone_kb() -> list:
    """Кнопка запроса геопозиции (reply-кнопка «Локація»).

    ВАЖНО: reply-кнопки нельзя смешивать с inline в одном сообщении
    (Telegram API: «You cannot mix inline with normal buttons»). Раньше
    сюда добавлялась inline-кнопка «Главное меню» → кнопка «Моя зона»
    падала с ошибкой каждый раз. Inline-меню рисуется отдельным
    сообщением из обработчика.
    """
    return [[_geo_button("📎 Надіслати локацію")]]


def _shelter_text(db: Database, user_id: int) -> str:
    """Экран «🛡 Укриття»: режим «только тревога + відбій»."""
    state = db.get_shelter_mode(user_id)
    if state:
        return (
            "🛡 <b>Режим «Укриття» — увімкнено</b>\n\n"
            "У ЛС приходитимуть лише: 🚨 тривога по твоїх регіонах, "
            "🟢 відбій та повідомлення з позначкою 🎯 (ваша зона).\n"
            "Шум (ППО, розвідка, артилерія) глушиться, поки ти в укритті.\n\n"
            "💡 Відбій і тривога — управляючі повідомлення, вони проходять "
            "завжди, навіть з увімкненим режимом."
        )
    return (
        "🛡 <b>Режим «Укриття» — вимкнено</b>\n\n"
        "Одна кнопка для часу в укритті: увімкни, коли зайшов, — і в ЛС "
        "залишиться лише тривога та відбій по твоїх регіонах. Увімкнеш "
        "назад, коли вийдеш, — повернуться всі типи.\n\n"
        "Не замінює фільтр «Типи тривог» — це тимчасовий режим на одну "
        "тривогу."
    )


def _shelter_kb(db: Database, user_id: int) -> list:
    state = db.get_shelter_mode(user_id)
    return [
        [Button.inline(
            ("🔕 Вимкнути режим" if state else "🛡 Увімкнути режим"),
            data=CB_SHELTER_TOGGLE,
        )],
        [Button.inline("🏠 Главное меню", data=CB_MAIN)],
    ]


def _week_text(db: Database, slug: str) -> str:
    """Недельный график тревог региона юникод-барами ▁▂▃▄▅▆▇█."""
    name = region_name(slug)
    counts = db.alert_counts_by_day(slug, days=7)
    if not counts:
        return (
            f"📈 <b>{name}</b> — тривоги за 7 днів\n\n"
            "Даних поки немає: епізоди тривог фіксуються з моменту запуску."
        )
    total = sum(c for _, c in counts)
    peak = max(c for _, c in counts) if total else 0
    if peak == 0:
        bars_line = " ".join("▁" for _ in counts)
    else:
        bars_line = " ".join(
            WEEK_BARS[min(len(WEEK_BARS) - 1, round(c / peak * (len(WEEK_BARS) - 1)))]
            for _, c in counts
        )
    labels_line = " ".join(label[:5] for label, _ in counts)
    lines = [
        f"📈 <b>{name}</b> — тривоги за 7 днів\n",
        f"<code>{bars_line}</code>",
        f"<code>{labels_line}</code>",
        f"\nВсього епізодів: <b>{total}</b>",
        "Висота стовпчика — кількість тривог за день (відносно найгіршого дня).",
    ]
    return "\n".join(lines)


def _stats_all_text(db: Database) -> str:
    """Компактная сводка: сейчас + подтверждённые инциденты + метрики."""
    now = int(time.time())
    day = now - 86400
    week = now - 7 * 86400

    active = db.active_threats(within_seconds=1800)
    active_regions = ", ".join(region_name(r["region"]) for r in active[:10]) or "немає"

    # Открытая метрика точности: опережение официальной сирены за неделю.
    lead = db.siren_lead_stats(days=7)
    lead_line = ""
    if lead["episodes"]:
        lead_min = lead["avg_lead_sec"] / 60
        lead_line = (
            f"\n⚡ Випередження сирени (7 днів): <b>{lead['before_count']}/{lead['episodes']}</b>"
            + (f", у середньому на <b>{lead_min:.0f} хв</b> раніше" if lead_min > 0 else "")
        )

    # Публичная точность прогнозов (публикуем открыто, раз в неделю
    # попадает в дайджест): «X% відбоїв у межах медіани». Конкуренты
    # свою точность не показывают — это доверие к бренду.
    try:
        from wave_forecast import public_accuracy
        acc = public_accuracy(db)
        if acc.get("available"):
            pct = round(100 * acc["hits"] / acc["total"])
            lead_line += (
                f"\n🎯 Точність відбоїв: <b>{pct}%</b> у межах медіани ({acc['hits']}/{acc['total']})"
            )
    except Exception:  # noqa: BLE001 — метрика не роняет статистику
        logging.getLogger(__name__).warning("подавлена помилка (%s:%d)", 'bot_ui.py', 511, exc_info=True)

    def inline_counts(since: float) -> str:
        counts = db.confirmed_incident_counts(region=None, since=int(since))
        total = sum(counts.values())
        if not total:
            return "нічого не зафіксовано"
        body = " · ".join(
            f"{TYPE_LABELS.get(t, t).split(' ')[0]} {c}"
            for t, c in sorted(counts.items(), key=lambda x: -x[1])
        )
        return f"{body} — <b>разом {total}</b>"

    return (
        "📊 <b>Статистика AirRadar</b>\n\n"
        f"🔴 Зараз (30 хв): <b>{len(active)}</b>"
        + (f"\n{active_regions}" if active else "")
        + "\n\n🚀 <b>Підтверджені інциденти</b>\n"
        f"• 24 год: {inline_counts(day)}\n"
        f"• 7 днів: {inline_counts(week)}"
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
            f"— {ago} мин назад\n   <i>{html.escape(public_text(t.get('text') or '', 60))}</i>"
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

    # Закриття застарілих статусів — у фоновому циклі (expire_alerts_loop), не як побічний ефект читання екрана.
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
        when = kyiv_fmt(it["ts"], "%d.%m %H:%M")
        lines.append(f"{TYPE_LABELS.get(it['type'], '🚨')} {when}\n   <i>{html.escape(public_text(it.get('text') or '', 70))}</i>")
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
        when = kyiv_fmt(it["ts"], "%d.%m %H:%M")
        lines.append(
            f"💥 {when} · {it['source_count']} незалежних джерел\n"
            f"   <i>{html.escape(public_text(it.get('text') or '', 160))}</i>"
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
        logging.getLogger(__name__).warning("подавлена помилка (%s:%d)", 'bot_ui.py', 626, exc_info=True)
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
