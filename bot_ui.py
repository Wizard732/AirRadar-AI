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
from telethon.tl.types import KeyboardButtonWebView

from database import Database
from eta import estimate_eta, format_eta
from regions import REGIONS, all_region_slugs, region_name

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
CB_MAIN = "main"         # главное меню
CB_STATS_ALL = "sall"    # общая статистика
CB_ACTIVE = "active"     # текущие угрозы

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


# =====================================================================
#  Сборка клавиатур
# =====================================================================

CB_INTERESTS = "interests"  # переход в Interests-модуль


def _main_menu_kb():
    return [
        [Button.inline("🪖 Военные алерты", data=CB_REGION_PAGE + "0")],
        [Button.inline("📰 Новости по интересам", data=CB_INTERESTS)],
        [
            Button.inline("📊 Общая статистика", data=CB_STATS_ALL),
            Button.inline("📋 Детальна статистика", data="stats_detail"),
        ],
        [Button.inline("⏱ Текущие угрозы", data=CB_ACTIVE)],
    ]


def _main_menu_with_webapp(webapp_url: str):
    """Главное меню + кнопка-WebApp (Mini App открывается по URL).

    Telethon 1.44 не имеет Button.webapp — используем KeyboardButtonWebView
    напрямую (это и есть нативный тип Telegram для Web App кнопок).
    """
    return [
        [KeyboardButtonWebView(text="⚙️ Настройки (Mini App)", url=webapp_url)],
        [Button.inline("🪖 Военные алерты", data=CB_REGION_PAGE + "0")],
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


def _region_menu_kb(slug: str, subscribed: bool = False):
    """Меню конкретного региона. Кнопка подписки меняется в зависимости от статуса."""
    sub_btn = Button.inline(
        "🔕 Отписаться" if subscribed else "🔔 Подписаться",
        data=CB_REGION_SUB + slug,
    )
    return [
        [
            Button.inline("📊 Статистика тревог", data=CB_REGION_STATS + slug),
            Button.inline("⏱ ETA угроз", data=CB_REGION_ETA + slug),
        ],
        [
            Button.inline("💥 История ударов", data=CB_REGION_HIST + slug),
            Button.inline("🔥 Последствия", data=CB_REGION_CONS + slug),
        ],
        [sub_btn],
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
        "ETA (время прилёта) и история ударов по областям Украины."
    )


def _region_menu_text(slug: str) -> str:
    return f"📍 <b>{region_name(slug)}</b>\n\nВыбери, что показать:"


def _stats_all_text(db: Database) -> str:
    """Сводка по всем регионам за разные периоды."""
    now = int(time.time())
    day = now - 86400
    week = now - 7 * 86400

    def render(since: float, label: str) -> str:
        counts = db.threat_counts(region=None, since=since)
        total = sum(counts.values())
        parts = [f"<b>{label}</b> (всего {total})"]
        for t, c in sorted(counts.items(), key=lambda x: -x[1]):
            parts.append(f"  {TYPE_LABELS.get(t, t)}: {c}")
        return "\n".join(parts) if total else f"<b>{label}</b>: данных пока нет"

    active = db.active_threats(within_seconds=1800)
    active_regions = ", ".join(region_name(r["region"]) for r in active[:10]) or "нет"

    return (
        "📊 <b>Общая статистика</b>\n\n"
        f"{render(day, 'За 24 часа')}\n\n"
        f"{render(week, 'За неделю')}\n\n"
        f"🔴 <b>Активные угрозы (30 мин):</b> {len(active)}\n"
        f"Регионы: {active_regions}"
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
        counts = db.threat_counts(region=slug, since=since)
        total = sum(counts.values())
        if not total:
            return f"<b>{label}</b>: данных пока нет"
        parts = [f"<b>{label}</b> (всего {total})"]
        for t, c in sorted(counts.items(), key=lambda x: -x[1]):
            parts.append(f"  {TYPE_LABELS.get(t, t)}: {c}")
        return "\n".join(parts)

    avg = db.avg_alert_duration(slug)
    if avg is not None:
        mins = avg / 60
        avg_str = f"~{mins:.0f} мин" if mins < 60 else f"~{mins / 60:.1f} ч"
        avg_line = f"Средняя длительность тревоги: <b>{avg_str}</b>"
    else:
        avg_line = "Средняя длительность тревоги: недостаточно данных"

    is_active = slug in db.active_alert_regions()
    status = "🔴 Подтверждённая тревога активна" if is_active else "⚪ Нет активного статуса от источников бота"

    return (
        f"📊 <b>{name}</b> — статистика\n\n"
        f"{render(day, 'За 24 часа')}\n\n"
        f"{render(week, 'За неделю')}\n\n"
        f"{avg_line}\n"
        f"Статус: {status}"
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
    """Показать сообщения о взрывах как непроверенные сообщения, не как факт удара."""
    name = region_name(slug)
    # Переиспользуем recent_threats, но фильтруем по типу explosion через отдельный запрос.
    items = [t for t in db.recent_threats(slug, limit=10) if t["type"] == "explosion"]
    if not items:
        return (
            f"🔥 <b>{name}</b> — последствия\n\n"
            "Сообщений о взрывах/прилётах пока нет.\n"
            "Это сообщения источников, а не независимое подтверждение последствий."
        )
    lines = [f"🔥 <b>{name}</b> — сообщения о последствиях\n"]
    for it in items[:5]:
        when = time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
        lines.append(f"💥 {when}\n   <i>{html.escape(it['text'][:70])}</i>")
    return "\n".join(lines)


def _region_eta_text(db: Database, slug: str) -> str:
    est = estimate_eta(db, slug)
    return format_eta(est, region_name(slug))


# =====================================================================
#  Регистрация обработчиков
# =====================================================================

def register_handlers(bot: TelegramClient, db: Database, admin_id: int, webapp_url: str = "") -> None:
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

    def _menu_kb():
        return _main_menu_with_webapp(webapp_url) if webapp_url else _main_menu_kb()

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start"))
    async def _start(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        # Меню доступно всем пользователям (подписки, статистика, алерты).
        await event.respond(_main_text(), parse_mode="html", buttons=_menu_kb())

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
                await _safe_edit(_main_text(), _menu_kb())

            elif data == CB_STATS_ALL:
                await _safe_edit(_stats_all_text(db), _menu_kb())

            elif data == "stats_detail":
                # Детальная статистика за неделю (все регионы).
                from stats import format_detailed_stats
                text = format_detailed_stats(db, days=7)
                await _safe_edit(text, _menu_kb())

            elif data == CB_ACTIVE:
                await _safe_edit(_active_text(db), _menu_kb())

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
                if slug in REGIONS:
                    sub = db.is_subscribed(event.sender_id, slug)
                    await _safe_edit(_region_menu_text(slug), _region_menu_kb(slug, sub))

            elif data.startswith(CB_REGION_STATS):
                slug = data[len(CB_REGION_STATS):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                await _safe_edit(_region_stats_text(db, slug), _region_menu_kb(slug, sub))

            elif data.startswith(CB_REGION_ETA):
                slug = data[len(CB_REGION_ETA):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                await _safe_edit(_region_eta_text(db, slug), _region_menu_kb(slug, sub))

            elif data.startswith(CB_REGION_HIST):
                slug = data[len(CB_REGION_HIST):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                await _safe_edit(_region_hist_text(db, slug), _region_menu_kb(slug, sub))

            elif data.startswith(CB_REGION_CONS):
                slug = data[len(CB_REGION_CONS):]
                await event.answer()
                sub = db.is_subscribed(event.sender_id, slug)
                await _safe_edit(_region_cons_text(db, slug), _region_menu_kb(slug, sub))

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
                await _safe_edit(_region_menu_text(slug), _region_menu_kb(slug, sub))

        except Exception as exc:  # noqa: BLE001 — не роняем меню на ошибке рендера
            logger.exception("Ошибка обработки callback %s: %s", data, exc)
            await event.answer("Ошибка отображения, смотри логи.", alert=True)
