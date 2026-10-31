"""user_extras.py — чек-лист безпеки, офлайн-пам'ятка та персональна статистика (Insights)."""
from __future__ import annotations

import time

CHECKLIST = [
    ("docs", "📄 Документи (паспорт, свідоцтва, поліси)"),
    ("water", "💧 Вода та їжа на 72 години"),
    ("meds", "💊 Ліки та аптечка"),
    ("power", "🔋 Заряджений телефон, павербанк"),
    ("light", "🔦 Ліхтарик і запасні батарейки"),
    ("cash", "💵 Готівка"),
    ("route", "🏚 Знаю найближче укриття й дорогу до нього"),
]
OFFLINE_TEXT = (
    "📴 <b>Офлайн-пам'ятка</b>\n\n"
    "• Екстрені служби: <b>112</b> (єдиний), <b>101</b> (ДСНС), <b>102</b> (поліція), <b>103</b> (швидка).\n"
    "• Почула сирену — одразу в укриття. Не стійте біля вікон; ідіть у приміщення без вікон між двома стінами.\n"
    "• Без інтернету орієнтуйтесь на сирену й офіційні оголошення. Збережіть тут список укриттів поруч — відкрийте «Укриття» "
    "та скопіюйте адреси, поки є зв'язок.\n\n"
    "ℹ️ Дані в боті мають час оновлення; офлайн вони можуть бути застарілими."
)


def _ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS checklist (user_id INTEGER NOT NULL, item TEXT NOT NULL, PRIMARY KEY(user_id,item))")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def toggle(db, uid: int, item: str) -> bool:
    if item not in dict(CHECKLIST):
        return False
    _ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM checklist WHERE user_id=? AND item=?", (uid, item))  # noqa: SLF001
        if cur.rowcount == 0:
            db._conn.execute("INSERT INTO checklist(user_id,item) VALUES (?,?)", (uid, item))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True


def done(db, uid: int) -> set[str]:
    _ensure(db)
    with db._lock:  # noqa: SLF001
        return {r[0] for r in db._conn.execute("SELECT item FROM checklist WHERE user_id=?", (uid,)).fetchall()}  # noqa: SLF001


def checklist_text(db, uid: int) -> str:
    d = done(db, uid)
    lines = [f"🎒 <b>Чек-лист безпеки</b> — готово {len(d)}/{len(CHECKLIST)}", ""]
    lines += [("✅ " if k in d else "⬜️ ") + label for k, label in CHECKLIST]
    lines.append("\nНатисни пункт, щоб відмітити.")
    return "\n".join(lines)


def _merge_intervals(rows, now: int) -> list[tuple[int, int]]:
    """Об'єднати перекриті тривоги (кілька областей/районів одночасно) в один проміжок — без подвійного обліку часу."""
    spans = sorted((int(st), int(en or now)) for st, en in rows)
    out: list[list[int]] = []
    for st, en in spans:
        if out and st <= out[-1][1]:
            out[-1][1] = max(out[-1][1], en)
        else:
            out.append([st, en])
    return [(a, b) for a, b in out]


def insights_text(db, uid: int, now: int | None = None, days: int = 7) -> str:
    """Скільки часу в регіонах користувача тривала офіційна тривога за період (за журналом alert_log)."""
    from personal_status import user_regions
    from regions import region_name
    now = int(now if now is not None else time.time())
    since = now - days * 86400
    regs = user_regions(db, uid)
    if not regs:
        return "📊 <b>Моя статистика</b>\n\nСпочатку підпишись на область або задай «Мою зону»."
    try:
        with db._lock:  # noqa: SLF001
            rows = db._conn.execute(  # noqa: SLF001
                "SELECT started_ts, ended_ts FROM alert_log WHERE started_ts>=? AND slug IN (%s)" % ",".join("?" * len(regs)),
                (since, *regs)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    merged = _merge_intervals(rows, now)
    count = len(merged)
    total = sum(b - a for a, b in merged)
    lines = [f"📊 <b>Моя статистика за {days} дн.</b>", ""]
    lines.append("Регіони: " + ", ".join(region_name(r) for r in regs[:3]))
    lines.append(f"Офіційних тривог: <b>{count}</b>")
    lines.append(f"Сумарна тривалість: <b>{total // 3600} год {total % 3600 // 60} хв</b>")
    lines.append("\n<i>Це статистика офіційних тривог у твоїх регіонах, а не прогноз.</i>")
    return "\n".join(lines)


# ---- щоденне нагадування чек-листа (опційно, лише для тих, хто ввімкнув) ----
REMIND_HOUR = 19   # за Києвом


def _ensure_remind(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS checklist_remind (user_id INTEGER PRIMARY KEY, last_day TEXT NOT NULL DEFAULT '')")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def remind_enabled(db, uid: int) -> bool:
    _ensure_remind(db)
    with db._lock:  # noqa: SLF001
        return db._conn.execute("SELECT 1 FROM checklist_remind WHERE user_id=?", (uid,)).fetchone() is not None  # noqa: SLF001


def toggle_remind(db, uid: int) -> bool:
    """Перемкнути нагадування; повертає новий стан."""
    _ensure_remind(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM checklist_remind WHERE user_id=?", (uid,))  # noqa: SLF001
        if cur.rowcount:
            db._conn.commit()  # noqa: SLF001
            return False
        db._conn.execute("INSERT INTO checklist_remind(user_id) VALUES (?)", (uid,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True


def due_reminders(db, now: int | None = None) -> list[int]:
    """Користувачі, яким пора надіслати нагадування (година REMIND_HOUR, раз на добу, чек-лист не завершений)."""
    from kyiv_time import kyiv_date, kyiv_hour
    now = int(now if now is not None else time.time())
    if kyiv_hour(now) != REMIND_HOUR:
        return []
    _ensure_remind(db)
    day = kyiv_date(now)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT user_id FROM checklist_remind WHERE last_day<>?", (day,)).fetchall()  # noqa: SLF001
    out = []
    for (uid,) in rows:
        if len(done(db, uid)) < len(CHECKLIST):
            out.append(uid)
        with db._lock:  # noqa: SLF001
            db._conn.execute("UPDATE checklist_remind SET last_day=? WHERE user_id=?", (day, uid))  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
    return out


async def reminder_loop(db, bot, interval: int = 600) -> None:
    import asyncio
    import logging
    while True:
        await asyncio.sleep(interval)
        try:
            for uid in due_reminders(db):
                await bot.send_message(uid, "🎒 Нагадування: перевір чек-лист безпеки — /checklist", silent=True)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("checklist reminder: збій", exc_info=True)


def calendar_text(db, uid: int, now: int | None = None, days: int = 14) -> str:
    """Календар офіційних тривог у регіонах користувача: по днях кількість і сумарна тривалість."""
    from kyiv_time import kyiv_date
    from personal_status import user_regions
    from regions import region_name
    now = int(now if now is not None else time.time())
    regs = user_regions(db, uid)
    if not regs:
        return "🗓 <b>Календар тривог</b>\n\nСпочатку підпишись на область або задай «Мою зону»."
    since = now - days * 86400
    try:
        with db._lock:  # noqa: SLF001
            rows = db._conn.execute(  # noqa: SLF001
                "SELECT started_ts, ended_ts FROM alert_log WHERE started_ts>=? AND slug IN (%s)" % ",".join("?" * len(regs)),
                (since, *regs)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    per: dict[str, list[int]] = {}
    for st, en in _merge_intervals(rows, now):
        d = per.setdefault(kyiv_date(st), [0, 0])
        d[0] += 1
        d[1] += max(0, en - st)
    lines = ["🗓 <b>Календар тривог</b> — " + ", ".join(region_name(r) for r in regs[:2]), ""]
    for i in range(days):
        day = kyiv_date(now - i * 86400)
        c, secs = per.get(day, [0, 0])
        bar = "▇" * min(c, 12) if c else "·"
        lines.append(f"{day[5:]} {bar} {c} · {secs // 3600}г {secs % 3600 // 60}хв" if c else f"{day[5:]} ·")
    lines.append("\n<i>Офіційні тривоги за журналом; це не прогноз.</i>")
    return "\n".join(lines)
