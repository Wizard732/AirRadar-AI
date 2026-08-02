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

import logging
import time

from telethon import Button, TelegramClient, events

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
CB_MAIN = "main"         #回到 главное меню
CB_STATS_ALL = "sall"    # общая статистика
CB_ACTIVE = "active"     # текущие угрозы

# Человекочитаемые подписи типов угроз.
TYPE_LABELS = {
    "missile": "🚀 Ракеты",
    "uav": "🛸 БПЛА",
    "explosion": "💥 Взрывы",
    "stand_down": "🟢 Отбои",
    "other": "🚨 Прочее",
}


# =====================================================================
#  Сборка клавиатур
# =====================================================================

def _main_menu_kb():
    return [
        [Button.inline("🏙 Выбрать область", data=CB_REGION_PAGE + "0")],
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


def _region_menu_kb(slug: str):
    """Меню конкретного региона."""
    return [
        [
            Button.inline("📊 Статистика тревог", data=CB_REGION_STATS + slug),
            Button.inline("⏱ ETA угроз", data=CB_REGION_ETA + slug),
        ],
        [
            Button.inline("💥 История ударов", data=CB_REGION_HIST + slug),
            Button.inline("🔥 Последствия", data=CB_REGION_CONS + slug),
        ],
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
        return "⏱ <b>Текущие угрозы</b>\n\nЗа последние 30 минут активных угроз не зафиксировано. ✅"
    lines = ["⏱ <b>Текущие угрозы</b> (за 30 мин)\n"]
    for t in threats[:15]:
        ago = int((time.time() - t["ts"]) / 60)
        lines.append(
            f"{TYPE_LABELS.get(t['type'], '🚨')} {region_name(t['region'])} "
            f"— {ago} мин назад\n   <i>{t['text'][:60]}</i>"
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
    status = "🔴 Тревога активна" if is_active else "🟢 Тихо"

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
        return f"💥 <b>{name}</b> — история ударов\n\nЗаписей пока нет."
    lines = [f"💥 <b>{name}</b> — последние удары\n"]
    for it in items:
        when = time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
        lines.append(f"{TYPE_LABELS.get(it['type'], '🚨')} {when}\n   <i>{it['text'][:70]}</i>")
    return "\n".join(lines)


def _region_cons_text(db: Database, slug: str) -> str:
    """Последствия: берём последние explosion-события в регионе как индикатор."""
    name = region_name(slug)
    # Переиспользуем recent_threats, но фильтруем по типу explosion через отдельный запрос.
    items = [t for t in db.recent_threats(slug, limit=10) if t["type"] == "explosion"]
    if not items:
        return (
            f"🔥 <b>{name}</b> — последствия\n\n"
            "Зафиксированных взрывов/прилетов пока нет.\n"
            "Бот отмечает последствия, когда в каналах появляются сообщения "
            "о взрывах в этом регионе."
        )
    lines = [f"🔥 <b>{name}</b> — зафиксированные последствия\n"]
    for it in items[:5]:
        when = time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
        lines.append(f"💥 {when}\n   <i>{it['text'][:70]}</i>")
    return "\n".join(lines)


def _region_eta_text(db: Database, slug: str) -> str:
    est = estimate_eta(db, slug)
    return format_eta(est, region_name(slug))


# =====================================================================
#  Регистрация обработчиков
# =====================================================================

def register_handlers(bot: TelegramClient, db: Database, admin_id: int) -> None:
    """Навесить на bot-клиента обработчики /start и нажатий кнопок.

    admin_id: Telegram user id, которому разрешено меню. Сообщения от других
    пользователей игнорируются (бот приватный).
    """

    def _is_admin(user_id: int) -> bool:
        return user_id == admin_id

    @bot.on(events.NewMessage(incoming=True, pattern=r"^/start"))
    async def _start(event: events.NewMessage.Event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            return
        await event.respond(_main_text(), parse_mode="html", buttons=_main_menu_kb())

    @bot.on(events.CallbackQuery())
    async def _callback(event) -> None:  # noqa: ANN001
        if not _is_admin(event.sender_id):
            await event.answer("Нет доступа.", alert=True)
            return

        data = event.data.decode("utf-8") if isinstance(event.data, bytes) else event.data

        try:
            # Маршрутизация по префиксу callback_data.
            if data == CB_MAIN:
                await event.edit(_main_text(), parse_mode="html", buttons=_main_menu_kb())

            elif data == CB_STATS_ALL:
                await event.edit(_stats_all_text(db), parse_mode="html", buttons=_main_menu_kb())

            elif data == CB_ACTIVE:
                await event.edit(_active_text(db), parse_mode="html", buttons=_main_menu_kb())

            elif data.startswith(CB_REGION_PAGE):
                page = int(data[len(CB_REGION_PAGE):] or "0")
                await event.edit(
                    "🏙 <b>Выбери область</b>\n\nЛистай кнопками ◀️ ▶️.",
                    parse_mode="html",
                    buttons=_regions_kb(page),
                )

            elif data.startswith(CB_REGION_SELECT):
                slug = data[len(CB_REGION_SELECT):]
                if slug in REGIONS:
                    await event.edit(
                        _region_menu_text(slug), parse_mode="html", buttons=_region_menu_kb(slug)
                    )

            elif data.startswith(CB_REGION_STATS):
                slug = data[len(CB_REGION_STATS):]
                await event.answer()
                await event.edit(
                    _region_stats_text(db, slug), parse_mode="html", buttons=_region_menu_kb(slug)
                )

            elif data.startswith(CB_REGION_ETA):
                slug = data[len(CB_REGION_ETA):]
                await event.answer()
                await event.edit(
                    _region_eta_text(db, slug), parse_mode="html", buttons=_region_menu_kb(slug)
                )

            elif data.startswith(CB_REGION_HIST):
                slug = data[len(CB_REGION_HIST):]
                await event.answer()
                await event.edit(
                    _region_hist_text(db, slug), parse_mode="html", buttons=_region_menu_kb(slug)
                )

            elif data.startswith(CB_REGION_CONS):
                slug = data[len(CB_REGION_CONS):]
                await event.answer()
                await event.edit(
                    _region_cons_text(db, slug), parse_mode="html", buttons=_region_menu_kb(slug)
                )

        except Exception as exc:  # noqa: BLE001 — не роняем меню на ошибке рендера
            logger.exception("Ошибка обработки callback %s: %s", data, exc)
            await event.answer("Ошибка отображения, смотри логи.", alert=True)
