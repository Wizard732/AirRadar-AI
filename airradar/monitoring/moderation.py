"""moderation.py — черга модерації помилок для адмінів. Виправлення стають прикладами для оцінки,
але модель автоматично НЕ донавчається (спочатку перевірка якості розмітки)."""
from __future__ import annotations

import json
import time

CATEGORY_UK = {
    "wrong_city": "не те місто", "wrong_type": "не той тип", "stale": "застаріло",
    "duplicate": "дублікат", "unclear": "незрозуміло",
}
DECISIONS = ("confirmed", "dismissed")


def open_reports(db, limit: int = 15) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT id, ts, user_id, message_key, category FROM error_reports WHERE status='open' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def resolve(db, report_id: int, decision: str) -> bool:
    if decision not in DECISIONS:
        return False
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("UPDATE error_reports SET status=? WHERE id=? AND status='open'", (decision, report_id))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount > 0


def queue_text(db) -> str:
    rows = open_reports(db)
    if not rows:
        return "✅ Черга модерації порожня."
    lines = ["🛠 <b>Черга модерації</b>"]
    for r in rows:
        lines.append(f"#{r['id']} · {CATEGORY_UK.get(r['category'], r['category'])} · <code>{r['message_key']}</code> · користувач <code>{r['user_id']}</code>")
    lines.append("\n/report_ok &lt;id&gt; — підтвердити помилку · /report_no &lt;id&gt; — відхилити · /export_reports — набір для оцінки\n/fix &lt;ключ&gt; region|type &lt;значення&gt; · /stale &lt;ключ&gt;")
    return "\n".join(lines)


def export_confirmed(db) -> str:
    """JSONL підтверджених помилок — вхід для benchmark/розмітки, не для автонавчання."""
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT id, ts, message_key, category FROM error_reports WHERE status='confirmed' ORDER BY id").fetchall()  # noqa: SLF001
    return "\n".join(json.dumps(dict(r), ensure_ascii=False) for r in rows)


FIX_FIELDS = {"region": "region", "type": "weapon_class"}


def _ensure_corrections(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS corrections (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, admin_id INTEGER NOT NULL, "
            "incident_key TEXT NOT NULL, field TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL)")
        db._conn.commit()  # noqa: SLF001


def apply_fix(db, admin_id: int, key: str, field: str, value: str) -> tuple[bool, str]:
    """Виправлення адміном: область або тип події. Кожна правка зберігається як приклад для оцінки парсера (corrections)."""
    from regions import all_region_slugs
    from weapon_classes import WEAPON_CLASSES
    column = FIX_FIELDS.get(field)
    if not column:
        return False, "Поле: region або type."
    if field == "region" and value not in all_region_slugs():
        return False, "Невідомий slug області."
    if field == "type" and value not in {c["slug"] for c in WEAPON_CLASSES}:
        return False, "Невідомий тип."
    _ensure_corrections(db)
    with db._lock:  # noqa: SLF001
        row = db._conn.execute(f"SELECT {column} FROM incidents WHERE incident_key=?", (key,)).fetchone()  # noqa: SLF001
        if row is None:
            return False, "Подію не знайдено."
        db._conn.execute(f"UPDATE incidents SET {column}=? WHERE incident_key=?", (value, key))  # noqa: SLF001
        db._conn.execute("INSERT INTO corrections(ts, admin_id, incident_key, field, old, new) VALUES (?,?,?,?,?,?)",  # noqa: SLF001
                         (int(__import__("time").time()), admin_id, key, field, str(row[0]), value))
        db._conn.commit()  # noqa: SLF001
    return True, f"✅ {field}: {row[0]} → {value}"


def mark_stale(db, key: str) -> bool:
    """Відмітити подію застарілою (життєвий цикл → Resolved)."""
    import event_lifecycle
    event_lifecycle._ensure(db)  # noqa: SLF001
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("UPDATE event_lifecycle SET stage='resolved', updated_ts=strftime('%s','now') WHERE event_key=? AND stage IN ('created','observed','verified','updated')", (key,))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount > 0
