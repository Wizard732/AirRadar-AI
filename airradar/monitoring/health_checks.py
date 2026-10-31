"""health_checks.py — діагностика джерел та конвеєра + синтетичний моніторинг.

Розрізняє три різні ситуації:
  * джерело недоступне (є помилки / вимкнене);
  * джерело активне, але нових повідомлень немає;
  * повідомлення приходять, але всі відфільтровані.
Синтетична подія ізольована: іде лише через safety_policy/notification_rules і ТІЛЬКИ адмінам.
"""
from __future__ import annotations

import collections
import time

OUTCOMES = ("received", "filtered", "duplicate", "disabled", "processed")
_COUNTERS: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
_SINCE = [time.time()]


def record(source: str, outcome: str) -> None:
    """Лічильник долі кожного вхідного поста (у пам'яті, з моменту запуску)."""
    if outcome in OUTCOMES and source:
        _COUNTERS[source][outcome] += 1


def reset() -> None:
    _COUNTERS.clear()
    _SINCE[0] = time.time()


SILENT_S = 3 * 3600


def diagnose_source(row: dict, counters: collections.Counter | None, now: float) -> tuple[str, str]:
    """(код, пояснення) для одного рядка channel_health."""
    if row.get("disabled"):
        return "disabled", "вимкнено адміном"
    c = counters or collections.Counter()
    recv = c["received"]
    if recv and c["processed"] == 0 and c["filtered"] + c["duplicate"] >= recv:
        return "all_filtered", f"{recv} постів отримано, усі відфільтровані"
    last = row.get("last_seen")
    if row.get("error_count", 0) >= 5 and (not last or now - last > SILENT_S):
        return "unavailable", f"помилки ({row['error_count']}), нових даних немає"
    if last and now - last > SILENT_S:
        return "silent", f"активний, але нових повідомлень {int((now - last) // 3600)} год"
    if not last and not recv:
        return "no_data", "ще не було повідомлень"
    return "ok", "працює"


def source_report(db, now: float | None = None) -> list[tuple[str, str, str]]:
    now = (time.time() if now is None else now)
    return [(r["channel"], *diagnose_source(r, _COUNTERS.get(r["channel"]), now)) for r in db.all_channel_health()]


def health_text(db, now: float | None = None) -> str:
    rep = source_report(db, now)
    icons = {"ok": "✅", "silent": "🔇", "all_filtered": "🧹", "unavailable": "🔴", "disabled": "🚫", "no_data": "❔"}
    lines = ["🩺 <b>Здоров'я системи</b>", ""]
    for ch, code, why in rep:
        lines.append(f"{icons.get(code, '•')} <code>{ch}</code> — {why}")
    if not rep:
        lines.append("Даних про джерела ще немає.")
    mins = int((time.time() - _SINCE[0]) // 60)
    totals = collections.Counter()
    for c in _COUNTERS.values():
        totals.update(c)
    lines += ["", f"Постів за {mins} хв з запуску: " + ", ".join(f"{k}={totals[k]}" for k in OUTCOMES)]
    d = db.delivery_summary(int(time.time()) - 86400)
    lines.append("Доставка за добу: " + (", ".join(f"{k}={v}" for k, v in d.items()) or "—"))
    return "\n".join(lines)


SYNTHETIC_TEXT = "🧪 ТЕСТ AirRadar: синтетична перевірка конвеєра. Це не тривога."


def synthetic_check() -> dict:
    """Прогнати тестову подію через детерміновані етапи. Нічого не публікує й не пише в БД."""
    from notification_rules import Change, Importance, importance_for, user_wants
    from safety_policy import check_publication
    verdict = check_publication(SYNTHETIC_TEXT)
    imp = importance_for(Change.NEW_EVENT)
    return {
        "safety_policy": verdict.allowed,
        "importance": imp.value,
        "routing": user_wants(imp, {Importance.INFO}),
        "isolated": True,   # тестова подія не потрапляє в канал і підписникам
    }


async def delivery_probe(db, bot, uid: int, now: int | None = None) -> dict:
    """Реальна доставка тестового сповіщення ОДНОМУ адміну: Telegram + ntfy/Android (якщо прив'язано).

    Ізоляція: отримувач — лише адмін, що запустив перевірку; підписники й канал не зачіпаються; текст явно тестовий.
    """
    import push_fcm
    now = int(now if now is not None else time.time())
    key = f"selftest-{now}"
    out = {"telegram": False, "android": None}
    db.record_delivery(key, f"tg:{uid}", "info", "created")
    try:
        await bot.send_message(uid, SYNTHETIC_TEXT)
        out["telegram"] = db.record_delivery(key, f"tg:{uid}", "info", "sent")
    except Exception:  # noqa: BLE001
        db.record_delivery(key, f"tg:{uid}", "info", "failed")
    topics = push_fcm.topics_of(db, uid) if push_fcm.configured() else []
    if topics:
        ok = 0
        for t in topics:
            ok += 1 if await push_fcm.send_topic(t, "ТЕСТ AirRadar", SYNTHETIC_TEXT, "info") else 0
        out["android"] = ok > 0
        db.record_delivery(key, f"ntfy:{uid}", "info", "sent" if ok else "failed")
    return out
