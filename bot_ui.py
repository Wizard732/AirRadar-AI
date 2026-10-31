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
        [t(RB_STATS), t(RB_HIST), t(RB_IMP)],
        [t(RB_GUARD), t(RB_PLACES), t(RB_SHEL)],
        [t(RB_FAMILY), t(RB_SAFE), t(RB_APP)],
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
        [Button.inline("📊 Статистика", data=ms.CB_STATS + "today"), Button.inline("💥 Історія атак", data=ms.CB_HISTORY)],
        [Button.inline("🔥 Наслідки", data=ms.CB_IMPACTS), Button.inline("🛡 Гео-сторож", data=ms.CB_GUARD)],
        [Button.inline("🎯 Моя зона", data=CB_MY_ZONE), Button.inline("🛡 Укриття", data=CB_SHELTER)],
        [Button.inline("🏚 Найближче укриття", data=ms.CB_SHELTER_FIND), Button.inline("📍 Мої місця", data=ms.CB_PLACES)],
        [Button.inline("🛡 Мій AirRadar", data="mys"), Button.inline("🎒 Чек-лист", data="chk")],
        [Button.inline("📊 Моя статистика", data="ins"), Button.inline("📴 Офлайн-пам'ятка", data="off")],
        [Button.inline("🔔 Типи сповіщень", data="lvl"), Button.inline("🗓 Календар тривог", data="cal")],
        [Button.inline("🔊 Голосова зведення", data="voi")],
        [Button.inline("📱 Додаток", data=ms.CB_APP)],
        [Button.inline("👪 Родина", data=ms.CB_FAMILY), Button.inline("✅ Я в укритті", data=f"{ms.CB_FAMILY}:safe")],
        [Button.inline("🔔 Мої підписки", data=CB_MY_SUBS), Button.inline("🏙 Області", data=CB_REGION_PAGE + "0")],
        [Button.inline("🎯 Типи тривог", data=CB_WEAPONS)],
        [Button.inline("📍 Повідомити загрозу", data=CB_REPORT), Button.inline("📰 Новини", data=CB_INTERESTS)],
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
        pass

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

# Пользовательские команды для «синей кнопки меню» Telegram (0 ввода текста).
BOT_COMMANDS = [
    ("start", "Головне меню (кнопки)"),
    ("map", "Карта загроз"),
    ("guard", "Гео-сторож"),
    ("city", "Підписка: місто або район"),
    ("report", "Повідомити загрозу"),
]


async def _register_bot_commands(bot: TelegramClient, app_url: str = "") -> None:
    """Зарегистрировать команды меню бота (best effort, с ожиданием коннекта)."""
    from telethon.tl.functions.bots import SetBotCommandsRequest
    from telethon.tl.types import BotCommand, BotCommandScopeDefault

    commands = [BotCommand(command=name, description=desc) for name, desc in BOT_COMMANDS]
    for _ in range(60):
        try:
            if not bot.is_connected():
                await asyncio.sleep(2)
                continue
            await bot(SetBotCommandsRequest(
                scope=BotCommandScopeDefault(), lang_code="", commands=commands,
            ))
            logger.info("Bot commands зарегистрированы: меню кнопкой, без ввода текста")
            if app_url:
                # «Синяя кнопка» сбоку от поля ввода открывает Mini App (карта, статистика, история, наслідки).
                try:
                    from telethon.tl.functions.bots import SetBotMenuButtonRequest
                    from telethon.tl.types import BotMenuButton, InputUserEmpty
                    await bot(SetBotMenuButtonRequest(user_id=InputUserEmpty(), button=BotMenuButton(text="📱 Карта", url=app_url)))
                    logger.info("Кнопка меню бота → Mini App: %s", app_url)
                except Exception as exc:  # noqa: BLE001 — не критично
                    logger.warning("Не удалось поставить кнопку Mini App в меню бота: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001 — не критично для работы меню
            logger.debug("set_bot_commands повтор: %s", exc)
            await asyncio.sleep(2)
    logger.warning("Не удалось зарегистрировать bot commands (меню работает по /start)")


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
        rows = _main_menu_with_webapp(webapp_url, map_webapp_url)
        # Персональный ночной режим: 23:00–06:00 в ЛС только критичные классы.
        state = "увімкнено ✅" if db.get_night_mode(user_id) else "вимкнено"
        return rows + [[Button.inline(f"🌙 Нічний режим: {state}", data=CB_NIGHT_MODE)]]

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/map(?:@\w+)?\s*$"))
    async def _map_cmd(event) -> None:  # noqa: ANN001
        base = ms.site_base(map_webapp_url)
        if not base:
            await event.respond("Карта тимчасово недоступна.")
            return
        await event.respond("🗺 <b>Карта загроз</b>", parse_mode="html", buttons=[
            [_webapp_button("🗺 Відкрити карту", base + "/"), _webapp_button("📱 Mini App", ms.app_url(map_webapp_url))],
            [Button.url("🌐 У браузері", base + "/")]])

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start"))
    async def _start(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        # В группах/каналах меню не показываем: бота там используют только
        # для /city (подписка чата). Короткое приветствие без кнопок.
        if not getattr(event, "is_private", True):
            await event.respond(
                "👋 Це AirRadar AI — бот тривог.\n\n"
                "Повне меню працює в особистих повідомленнях: натисніть "
                "<code>/start</code> у ЛС бота.\n\n"
                "Тут, у чаті, можна підписати весь чат на алерти: "
                "<code>/city Дарниця</code> — повторна команда відписує.",
                parse_mode="html",
            )
            return
        # Меню доступно всем пользователям (подписки, статистика, алерты).
        try:
            import family
            sender = await event.get_sender()
            family.remember_name(db, event.sender_id, getattr(sender, "first_name", "") or getattr(sender, "username", "") or "")
        except Exception:  # noqa: BLE001 — ім'я не критичне
            pass
        await event.respond(_main_text(), parse_mode="html", buttons=_menu_kb(event.sender_id))
        # Бабушка-режим: постоянная reply-клавиатура с большими кнопками —
        # всегда на экране, вводить текст не нужно вообще. Отдельным
        # сообщением: reply-кнопки нельзя смешивать с inline.
        try:
            import family as _fam
            _fam.set_kb_current(db, event.sender_id)
            await event.respond("⬇️ Кнопки всегда под клавиатурой:", buttons=_persistent_kb())
        except Exception:  # noqa: BLE001 — reply-клавиатура не критична
            pass
    # Фулл управление кнопками (0 ввода текста): текстовые reply-кнопки
    # (бабушка-режим) выполняют действие сразу; любой другой обычный текст
    # открывает главное меню. Команды (/…), JSON из Mini App и сообщения
    # без текста (геопозиция) обрабатывают свои хендлеры — сюда не попадают.
    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _any_text_menu(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        raw = (getattr(event.message, "text", "") or "").strip()
        if not raw or raw.startswith("/") or raw.startswith('{"action"'):
            return
        raw = RB_LEGACY.get(raw, raw)
        uid = event.sender_id
        # Нова розкладка нижньої клавіатури: старим користувачам оновлюємо її автоматично при першому натисканні.
        try:
            import family
            if family.kb_outdated(db, uid):
                family.set_kb_current(db, uid)
                await event.respond("🔄 Меню оновлено: нові кнопки внизу (Місця, Укриття поруч, Родина, Додаток).", buttons=_persistent_kb())
        except Exception:  # noqa: BLE001
            logger.debug("kb refresh", exc_info=True)
        if raw == RB_MAP:
            base = ms.site_base(map_webapp_url)
            if base:
                await event.respond("🗺 <b>Карта загроз</b>", parse_mode="html", buttons=[
                    [_webapp_button("🗺 Відкрити карту", base + "/"), _webapp_button("📱 Mini App", ms.app_url(map_webapp_url))],
                    [Button.url("🌐 У браузері", base + "/")]])
            else:
                await event.respond("Карта тимчасово недоступна.", buttons=_menu_kb(uid))
            return
        if raw == RB_SAFE:
            import family
            family.set_safe(db, uid)
            n = 0
            if getattr(bot, "send_message", None) is not None:
                async def _send(to, msg):
                    await bot.send_message(to, msg, silent=False)
                n = await family.notify_members(db, _send, uid, family.safe_text(db, uid))
            await event.respond("✅ Відмічено: ти в укритті." + (f" Родину сповіщено ({n})." if n else "")
                                + " Повтори попередження вимкнено на 3 години.")
            return
        screen = None
        if raw == RB_PLACES:
            screen = ms.places_screen(db, uid)
        elif raw == RB_SHEL:
            screen = ms.shelter_pick_screen(db, uid)
        elif raw == RB_FAMILY:
            import family
            screen = ms.family_screen(db, uid)
        elif raw == RB_APP:
            screen = ms.app_screen(db, uid, map_webapp_url)
        elif raw == RB_TRACKS:
            screen = ms.tracks_screen(db, map_webapp_url)
        elif raw == RB_HIST:
            screen = ms.history_screen(db, map_webapp_url)
        elif raw == RB_IMP:
            screen = ms.impacts_screen(db, map_webapp_url)
        elif raw == RB_GUARD:
            screen = ms.guard_screen(db, uid, map_webapp_url)
        if screen is not None:
            await event.respond(screen[0], parse_mode="html", buttons=screen[1])
            return
        # Текстовые reply-кнопки: действие сразу, без меню и без вопросов.
        if raw == RB_ACTIVE:
            await event.respond(_active_text(db), parse_mode="html", buttons=_menu_kb(event.sender_id))
            return
        if raw == RB_REGIONS:
            await event.respond(
                "🏙 <b>Выбери область</b>\n\nЛистай кнопками ◀️ ▶️.",
                parse_mode="html",
                buttons=_regions_kb(0),
            )
            return
        if raw == RB_STATS:
            text, rows = ms.stats_screen(db, "today", map_webapp_url)
            await event.respond(text, parse_mode="html", buttons=rows)
            return
        if raw == RB_ZONE:
            pending = getattr(bot, "_airradar_zone_pending", None)
            if pending is None:
                pending = bot._airradar_zone_pending = {}
            pending[event.sender_id] = time.time() + ZONE_PROMPT_TTL
            await event.respond(_zone_text(db, event.sender_id), parse_mode="html")
            try:
                await event.respond("📎 Нажми кнопку и отправь локацию:", buttons=_zone_kb())
            except Exception:  # noqa: BLE001
                pass
            return
        if raw == RB_SHELTER:
            state = db.get_shelter_mode(event.sender_id)
            db.set_shelter_mode(event.sender_id, not state)
            await event.respond(_shelter_text(db, event.sender_id), parse_mode="html")
            return
        if raw == RB_SUBS:
            subs = db.user_subscriptions(event.sender_id)
            await event.respond(_my_subs_text(subs), parse_mode="html", buttons=_my_subs_kb(subs))
            return
        if raw == RB_HELP:
            await event.respond(_main_text(), parse_mode="html", buttons=_menu_kb(event.sender_id))
            return
        # Любой другой текст → главное меню (и подсказка повторно).
        await event.respond(
            "Керуйте кнопками нижче — текстові команди не потрібні.",
            parse_mode=None,
            buttons=_menu_kb(event.sender_id),
        )

    # Команды в «синей кнопке меню» Telegram: пользователь видит действия
    # списком и запускает тапом — вводить текст не нужно вовсе.
    # create_task требует активного loop: в боевом запуске (main.run) он есть,
    # а вне loop (юнит-тесты) задачу не планируем — иначе RuntimeError.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        bot._airradar_commands_task = None
    else:
        bot._airradar_commands_task = loop.create_task(_register_bot_commands(bot, ms.app_url(map_webapp_url)))

    # Геопозиция в ЛС: если пользователь перед этим нажал «🎯 Моя зона» —
    # сохраняем домашний регион и глотаем событие (events.StopPropagation),
    # чтобы оно не ушло в geo_report как репорт угрозы. Хендлер регистрируется
    # до geo_report (register_handlers вызывается раньше).
    @bot.on(
        events.NewMessage(
            incoming=True,
            func=lambda e: getattr(getattr(e, "message", None), "geo", None) is not None
            and e.is_private,
        )
    )
    async def _zone_geo(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        uid = event.sender_id
        pending = getattr(bot, "_airradar_zone_pending", None)
        deadline = (pending or {}).get(uid, 0)
        if not pending or deadline < time.time():
            return  # запроса не было/истёк — работает обычный geo-репорт
        pending.pop(uid, None)
        geo = event.message.geo
        lat, lon = float(geo.lat), float(geo.long)
        slug = region_for_point(lat, lon)
        if slug:
            db.set_home_region(uid, slug)
            logger.info("Моя зона %s: %s (%.3f, %.3f)", uid, slug, lat, lon)
            await event.respond(
                f"🎯 <b>Зону збережено: {html.escape(region_name(slug))}</b>\n\n"
                "Алерти, що торкаються цієї області, отримуватимуть позначку "
                "«🎯 Торкнеться вашої зони».",
                parse_mode="html",
            )
        else:
            await event.respond(
                "Не вдалося визначити регіон за цією точкою. Спробуй ще раз "
                "або обери область через <b>Военные алерты</b>.",
                parse_mode="html",
            )
        raise events.StopPropagation

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/city\s+(.+)$"))
    async def _city(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        """Подписка на регион/берег по названию города: /city Дарниця.

        Находит подходящие регионы/зоны и сразу подписывает (toggle —
        повторная команда отписывает). Ответ — кнопки, чтобы можно было
        открыть меню региона или отписаться.

        В личке подписывается пользователь (sender_id), в группах — весь
        чат (chat_id), чтобы алерты падали в общий чат.
        """
        query = (event.pattern_match.group(1) or "").strip()
        matches = find_regions_by_text(query)
        is_private = getattr(event, "is_private", True)
        target = event.sender_id if is_private else getattr(event, "chat_id", event.sender_id)
        if not matches:
            await event.respond(
                f"🔎 Не нашёл «{html.escape(query)}». Попробуй область или район, "
                "например: <code>/city Дніпро</code>, <code>/city Дарниця</code>.",
                parse_mode="html",
            )
            return
        # Подписываем на первый (самый точный) результат, остальные — кнопками.
        slug = matches[0]
        if db.is_subscribed(target, slug):
            db.unsubscribe(target, slug)
            answer = "🔕 Отписка от " + region_name(slug)
        else:
            db.subscribe(target, slug)
            answer = (
                "🔔 Подписка на " + region_name(slug)
                if is_private
                else "🔔 Чат підписано на " + region_name(slug)
            )
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

            elif data == ms.CB_TRACKS:
                await event.answer()
                text, rows = ms.tracks_screen(db, map_webapp_url)
                await _safe_edit(text, rows)

            elif data.startswith(ms.CB_STATS):
                await event.answer()
                text, rows = ms.stats_screen(db, data[len(ms.CB_STATS):], map_webapp_url)
                await _safe_edit(text, rows)

            elif data == ms.CB_HISTORY:
                await event.answer()
                text, rows = ms.history_screen(db, map_webapp_url)
                await _safe_edit(text, rows)

            elif data == ms.CB_IMPACTS:
                await event.answer()
                text, rows = ms.impacts_screen(db, map_webapp_url)
                await _safe_edit(text, rows)

            elif data == ms.CB_FAMILY or data.startswith(ms.CB_FAMILY + ":"):
                import family
                parts = data.split(":")
                notice = ""
                uid_ = event.sender_id
                if len(parts) > 1 and parts[1] == "inv":
                    code = family.create_invite(db, uid_)
                    try:
                        uname = (await bot.get_me()).username or ""
                    except Exception:  # noqa: BLE001
                        uname = ""
                    link = f"https://t.me/{uname}?start=fam_{code}" if uname else f"/start fam_{code}"
                    await event.answer("Запрошення створено")
                    await event.respond("👪 Надішли це посилання близькій людині (діє 48 годин, одноразове):\n" + link, link_preview=False)
                elif len(parts) > 2 and parts[1] == "del" and parts[2].lstrip("-").isdigit():
                    other = int(parts[2])
                    if family.remove_member(db, uid_, other):
                        notice = "🗑 Зв'язок видалено."
                    await event.answer(notice or "Не знайдено")
                elif len(parts) > 2 and parts[1] == "st":
                    import family_status as fst
                    if fst.set_status(db, uid_, parts[2]):
                        async def _send2(to, msg):
                            await bot.send_message(to, msg, silent=False)
                        n = await family.notify_members(db, _send2, uid_, fst.status_message(db, uid_, parts[2]))
                        await event.answer("Статус збережено" + (f"; родину сповіщено ({n})" if n else ""))
                        notice = "Статус: " + fst.STATUSES[parts[2]]
                    else:
                        await event.answer("Невідомий статус")
                elif len(parts) > 1 and parts[1] == "safe":
                    family.set_safe(db, uid_)
                    n = 0
                    if getattr(bot, "send_message", None) is not None:
                        async def _send(to, msg):
                            await bot.send_message(to, msg, silent=False)
                        n = await family.notify_members(db, _send, uid_, family.safe_text(db, uid_))
                    await event.answer("✅ Відмічено: ти в укритті" + (f"; родину сповіщено ({n})" if n else ""), alert=False)
                    notice = "✅ Відмічено, що ти в укритті — повтори сигналу вимкнено на 3 години."
                else:
                    await event.answer()
                if len(parts) == 1 or parts[1] in ("del", "safe", "st"):
                    text, rows = ms.family_screen(db, uid_, notice)
                    await _safe_edit(text, rows)

            elif data == "mys":
                await event.answer()
                import personal_status
                await _safe_edit(personal_status.render(db, event.sender_id), [[Button.inline("🔄 Оновити", data="mys"), Button.inline("🏠 Меню", data=CB_MAIN)]])

            elif data == "chk" or data.startswith("chk:"):
                import user_extras
                if data == "chk:remind":
                    user_extras.toggle_remind(db, event.sender_id)
                elif data.startswith("chk:"):
                    user_extras.toggle(db, event.sender_id, data[4:])
                await event.answer()
                done_ = user_extras.done(db, event.sender_id)
                rows_ = [[Button.inline(("✅ " if k in done_ else "⬜️ ") + label[2:].strip()[:40], data="chk:" + k)] for k, label in user_extras.CHECKLIST]
                rows_.append([Button.inline(("⏰ Нагадування о 19:00: увімкнено" if user_extras.remind_enabled(db, event.sender_id) else "⏰ Нагадувати щодня о 19:00"), data="chk:remind")])
                rows_.append([Button.inline("🏠 Меню", data=CB_MAIN)])
                await _safe_edit(user_extras.checklist_text(db, event.sender_id), rows_)

            elif data.startswith("ack:"):
                db.record_delivery(data[4:], f"tg:{event.sender_id}", "critical", "acknowledged")
                await event.answer("Дякуємо, відмічено")

            elif data == "lvl" or data.startswith("lvl:"):
                if data.startswith("lvl:"):
                    db.toggle_notify_level(event.sender_id, data[4:])
                await event.answer()
                off_ = db.get_notify_levels(event.sender_id)
                names_ = (("high", "Високий: зміни статусу"), ("info", "Інформаційні: зведення"), ("analytic", "Аналітичні: прогнози"))
                rows_ = [[Button.inline(("🔕 " if k in off_ else "🔔 ") + t, data="lvl:" + k)] for k, t in names_]
                rows_.append([Button.inline("🏠 Меню", data=CB_MAIN)])
                await _safe_edit("🔔 <b>Типи сповіщень</b>\n\n🚨 Критичні (офіційна тривога, відбій) приходять завжди — їх вимкнути не можна.\nНатисни, щоб увімкнути/вимкнути інші:", rows_)

            elif data == "ins":
                await event.answer()
                import user_extras
                await _safe_edit(user_extras.insights_text(db, event.sender_id), [[Button.inline("🏠 Меню", data=CB_MAIN)]])

            elif data == "cal":
                await event.answer()
                import user_extras
                await _safe_edit(user_extras.calendar_text(db, event.sender_id), [[Button.inline("🏠 Меню", data=CB_MAIN)]])

            elif data == "voi":
                await event.answer("Готую голосову зведення…")
                import io

                from airradar.product import voice
                text_ = voice.voice_text(db, event.sender_id)
                audio_ = await voice.synthesize(text_)
                if audio_ and getattr(bot, "send_file", None) is not None:
                    f_ = io.BytesIO(audio_)
                    f_.name = "airradar_voice.mp3"
                    await bot.send_file(event.sender_id, f_, caption=text_)
                else:
                    await event.respond("🔊 " + text_)

            elif data == "off":
                await event.answer()
                import user_extras
                await _safe_edit(user_extras.OFFLINE_TEXT, [[Button.inline("🏠 Меню", data=CB_MAIN)]])

            elif data == ms.CB_APP:
                await event.answer()
                text, rows = ms.app_screen(db, event.sender_id, map_webapp_url)
                await _safe_edit(text, rows)

            elif data.startswith(ms.CB_APP + ":g:"):
                await event.answer()
                try:
                    page = int(data.rsplit(":", 1)[1])
                except ValueError:
                    page = 0
                text, rows = ms.guide_screen(page, map_webapp_url)
                await _safe_edit(text, rows)

            elif data == ms.CB_PLACES:
                await event.answer()
                text, rows = ms.places_screen(db, event.sender_id)
                await _safe_edit(text, rows)

            elif data.startswith(ms.CB_PLACE_ACT):
                import guard_places
                parts = data[len(ms.CB_PLACE_ACT):].split(":")
                notice = ""
                if parts[0] == "add":
                    pend = getattr(bot, "_airradar_pending", None)
                    if pend is None:
                        pend = bot._airradar_pending = {}
                    kind = parts[1] if len(parts) > 1 else "other"
                    pend[event.sender_id] = ("place", ms.place_label(kind), time.time() + 300)
                    await event.answer()
                    await event.respond("📍 Напиши місто (наприклад: Харків) або надішли геопозицію кнопкою нижче. "
                                        "Для іншої назви: «Мама: Суми».")
                    try:
                        await event.respond("📎 Геопозиція:", buttons=_zone_kb())
                    except Exception:  # noqa: BLE001
                        pass
                    continue_edit = False
                else:
                    continue_edit = True
                    if parts[0] == "del" and len(parts) > 1 and parts[1].isdigit():
                        notice = "🗑 Місце видалено." if guard_places.remove_place(db, event.sender_id, int(parts[1])) else ""
                        await event.answer(notice or "Не знайдено")
                    elif parts[0] == "rad" and len(parts) > 1 and parts[1].isdigit() and int(parts[1]) in ms.RADII:
                        guard_places.set_radius(db, event.sender_id, float(parts[1]))
                        notice = f"✅ Радіус для всіх місць: {parts[1]} км"
                        await event.answer(notice)
                if continue_edit:
                    text, rows = ms.places_screen(db, event.sender_id, notice)
                    await _safe_edit(text, rows)

            elif data == ms.CB_SHELTER_FIND:
                await event.answer()
                text, rows = ms.shelter_pick_screen(db, event.sender_id)
                await _safe_edit(text, rows)

            elif data.startswith(ms.CB_SHELTER_FIND + ":"):
                token = data[len(ms.CB_SHELTER_FIND) + 1:]
                if token in ("loc", "city"):
                    pend = getattr(bot, "_airradar_pending", None)
                    if pend is None:
                        pend = bot._airradar_pending = {}
                    pend[event.sender_id] = ("shelter", token, time.time() + 300)
                    await event.answer()
                    if token == "loc":
                        await event.respond("📍 Надішли геопозицію кнопкою нижче — покажу найближчі укриття.")
                        try:
                            await event.respond("📎 Геопозиція:", buttons=_zone_kb())
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        await event.respond("🏙 Напиши назву міста або села — покажу найближчі укриття.")
                else:
                    pt = ms.shelter_point(db, event.sender_id, token)
                    if pt is None:
                        await event.answer("Точку не знайдено", alert=True)
                    else:
                        await event.answer("Шукаю укриття…")
                        text, rows = await ms.shelter_reply(pt[0], pt[1], pt[2])
                        await _safe_edit(text, rows)

            elif data == ms.CB_GUARD:
                await event.answer()
                text, rows = ms.guard_screen(db, event.sender_id, map_webapp_url)
                await _safe_edit(text, rows)

            elif data.startswith(ms.CB_GUARD_ACT):
                action = data[len(ms.CB_GUARD_ACT):]
                if action == "city":
                    pend = getattr(bot, "_airradar_city_pending", None)
                    if pend is None:
                        pend = bot._airradar_city_pending = {}
                    pend[event.sender_id] = time.time() + 300
                    await event.answer()
                    await event.respond("🏙 Напиши назву міста або села, за яким стежити (наприклад: Харків).")
                elif action == "test":
                    await event.answer("Надсилаю тестове сповіщення")
                    await event.respond(ms.test_alert_text(map_webapp_url))
                elif action == "loc":
                    await event.answer("Надішли геопозицію кнопкою нижче")
                    try:
                        await event.respond("📍 Натисни кнопку, щоб надіслати точку (для live: 📎 → Локація → «Транслювати»):", buttons=_zone_kb())
                    except Exception:  # noqa: BLE001
                        await event.respond("📎 → Локація → «Транслювати мою геопозицію»")
                else:
                    await event.answer(ms.guard_apply(db, event.sender_id, action))
                    text, rows = ms.guard_screen(db, event.sender_id, map_webapp_url)
                    await _safe_edit(text, rows)

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

            elif data == CB_MY_ZONE:
                # «Моя зона»: ждём геопозицию TTL секунд, потом сбрасываем.
                # Два сообщения: текст+inline-меню (HTML) и reply-кнопка
                # «Локація» — нельзя смешивать inline с reply в одном
                # сообщении (Telegram API), из-за этого кнопка не работала.
                pending = getattr(bot, "_airradar_zone_pending", None)
                if pending is None:
                    pending = bot._airradar_zone_pending = {}
                pending[event.sender_id] = time.time() + ZONE_PROMPT_TTL
                await event.answer()
                await event.respond(
                    _zone_text(db, event.sender_id),
                    parse_mode="html",
                    buttons=[Button.inline("🏠 Главное меню", data=CB_MAIN)],
                )
                try:
                    await event.respond("📎 Нажми кнопку и отправь локацию:", buttons=_zone_kb())
                except Exception:  # noqa: BLE001 — reply-кнопка не критична
                    pass

            elif data == CB_SHELTER:
                # Экран «Укриття»: состояние + toggle.
                await _safe_edit(
                    _shelter_text(db, event.sender_id),
                    _shelter_kb(db, event.sender_id),
                )

            elif data == CB_SHELTER_TOGGLE:
                new_state = not db.get_shelter_mode(event.sender_id)
                db.set_shelter_mode(event.sender_id, new_state)
                await event.answer(
                    "🛡 Укриття: лише тривога + відбій" if new_state
                    else "🛡 Режим укриття вимкнено"
                )
                await _safe_edit(
                    _shelter_text(db, event.sender_id),
                    _shelter_kb(db, event.sender_id),
                )

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

            elif data.startswith("fbu:") or data.startswith("fbn:") or data.startswith("fbe:"):
                # Фидбек под алертом в ЛС: «✅ корисно / ➖ шум / ❌ помилка».
                vote = (
                    "useful" if data.startswith("fbu:")
                    else "error" if data.startswith("fbe:")
                    else "noise"
                )
                key = data[4:].strip()
                if key:
                    db.add_alert_feedback(key, vote)
                if vote == "error" and key:
                    # Уточнюємо, що саме не так: категорія → черга модерації та датасет парсера.
                    await event.answer("Що саме не так?")
                    await event.respond(
                        "❌ <b>Що не так у сповіщенні?</b>",
                        parse_mode="html",
                        buttons=[
                            [Button.inline("📍 Не те місто", data=f"fbc:wrong_city:{key}"[:64])],
                            [Button.inline("🛩 Не той тип загрози", data=f"fbc:wrong_type:{key}"[:64])],
                            [Button.inline("⏳ Застаріла інформація", data=f"fbc:stale:{key}"[:64])],
                            [Button.inline("🔁 Дублікат", data=f"fbc:duplicate:{key}"[:64])],
                            [Button.inline("❓ Незрозуміле сповіщення", data=f"fbc:unclear:{key}"[:64])],
                        ],
                    )
                    return
                await event.answer(
                    "Дякуємо за відгук!" if vote == "useful"
                    else "Помилку зафіксовано — розберемось." if vote == "error"
                    else "Прийнято — працюємо над точністю."
                )

            elif data.startswith("fbc:"):
                _, _, rest = data.partition(":")
                cat, _, key = rest.partition(":")
                ok = bool(key) and db.add_error_report(event.sender_id, key, cat)
                await event.answer("Дякуємо! Звіт передано на перевірку." if ok else "Не вдалося записати звіт")
                if ok:
                    await _safe_edit("✅ Дякуємо! Звіт про помилку передано на перевірку.", None)
                    # Звіти бачать лише адміни.
                    from admin_notify import notify_admins
                    await notify_admins(
                        bot, db, admin_id,
                        f"📝 <b>Звіт про помилку</b>\nКористувач: <code>{event.sender_id}</code>\n"
                        f"Категорія: <b>{cat}</b>\nПост: <code>{key}</code>", parse_mode="html")

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

            elif data.startswith(CB_REGION_WEEK):
                slug = data[len(CB_REGION_WEEK):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                zones = None
                if slug == "kyivska":
                    zones = (
                        db.is_subscribed(event.sender_id, "kyiv_left"),
                        db.is_subscribed(event.sender_id, "kyiv_right"),
                    )
                await _safe_edit(_week_text(db, slug), _region_menu_kb(slug, sub, zones))

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
