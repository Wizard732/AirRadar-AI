"""ai_analyzer.py — ІІ-аналізатор усієї системи на локальній/підключеній моделі проєкту.

Збирає метрики конвеєра (затримка, джерела, рішення LLM-аудиту, звіти про помилки, доставка),
детермінованими правилами знаходить проблеми, а модель (summarizer.classify: Ollama/Groq) лише
формулює короткий висновок. Модель НІЧОГО не публікує й не змінює: звіт читають тільки адміни.
"""
from __future__ import annotations

import asyncio
import logging
import time

from admin_notify import notify_admins
from database import Database

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ти — інженер надійності системи сповіщень про повітряні загрози. За наданими метриками "
    "напиши українською короткий аналіз (до 6 пунктів): що працює погано, ймовірна причина, що перевірити. "
    "Формат: для КОЖНОЇ проблеми з переліку — один рядок «проблема → що перевірити». Використовуй лише надані дані, "
    "без загальних фраз і припущень, цифр не вигадуй. Не давай порад щодо безпеки населення — лише про роботу системи."
)


def make_llm(fallback=None, session=None):
    """Модель для аналізатора: API-посередник (ANALYZER_API_*, OpenAI-сумісний) або запасна модель проєкту."""
    import os
    key = os.getenv("ANALYZER_API_KEY", "").strip()
    if not key:
        return fallback
    from ai_summarizer import GroqSummarizer   # той самий OpenAI-сумісний клієнт chat/completions
    return GroqSummarizer(
        api_key=key, model=os.getenv("ANALYZER_MODEL", "claude-sonnet-5"), timeout=60,
        base_url=os.getenv("ANALYZER_API_URL", "https://ru.cheapvibecode.ru"), session=session)


def collect_metrics(db: Database, now: int | None = None, hours: int = 24) -> dict:
    now = int(now or time.time())
    since = now - hours * 3600
    m: dict = {"hours": hours}
    try:
        m["latency"] = db.latency_percentiles(seconds=hours * 3600)
    except Exception:  # noqa: BLE001
        m["latency"] = {}
    channels = db.all_channel_health()
    m["channels_total"] = len(channels)
    m["channels_disabled"] = [c["channel"] for c in channels if c["disabled"]]
    m["channels_errors"] = [(c["channel"], c["error_count"]) for c in channels if c["error_count"] > 0]
    m["channels_silent"] = [c["channel"] for c in channels
                            if c["last_seen"] and now - c["last_seen"] > 6 * 3600 and not c["disabled"]]
    m["llm_decisions"] = db.audit_summary(since)
    m["error_reports"] = db.error_report_counts()
    m["delivery"] = db.delivery_summary(since)
    return m


def find_issues(m: dict) -> list[str]:
    """Детерміновані правила — ядро аналізу; ІІ лише пояснює."""
    issues: list[str] = []
    lat = m.get("latency") or {}
    if lat.get("samples") and lat.get("p95_ms", 0) > 120_000:
        issues.append(f"Затримка конвейєра p95 {lat['p95_ms'] / 1000:.0f} с (>120 с)")
    if m["channels_disabled"]:
        issues.append("Вимкнені канали: " + ", ".join(m["channels_disabled"][:8]))
    if m["channels_silent"]:
        issues.append(f"Мовчать >6 год: {len(m['channels_silent'])} каналів ({', '.join(m['channels_silent'][:5])})")
    bad = [f"{c}×{n}" for c, n in m["channels_errors"] if n >= 5]
    if bad:
        issues.append("Часті помилки джерел: " + ", ".join(bad[:6]))
    dec = m["llm_decisions"]
    total = sum(dec.values())
    rej = dec.get("rejected", 0) + dec.get("ignored", 0)
    if total >= 20 and rej / total > 0.6:
        issues.append(f"LLM відхиляє {rej}/{total} постів ({rej * 100 // total}%) — перевірити фільтри/модель")
    rep = m["error_reports"]
    if sum(rep.values()) >= 3:
        top = max(rep, key=rep.get)
        issues.append(f"Звіти користувачів про помилки: {sum(rep.values())} (найчастіше: {top})")
    d = m["delivery"]
    sent = d.get("sent", 0) + d.get("delivered", 0) + d.get("opened", 0) + d.get("acknowledged", 0)
    if d.get("failed", 0) and d["failed"] > max(3, sent * 0.1):
        issues.append(f"Невдалих доставок {d['failed']} проти {sent} успішних")
    return issues


def render_metrics(m: dict) -> str:
    lat = m.get("latency") or {}
    lines = [f"Період: {m['hours']} год", f"Каналів: {m['channels_total']}"]
    if lat.get("samples"):
        lines.append(f"Затримка p50 {lat['p50_ms'] / 1000:.1f} с, p95 {lat['p95_ms'] / 1000:.1f} с ({lat['samples']} замірів)")
    lines.append(f"Рішення LLM: {m['llm_decisions'] or '—'}")
    lines.append(f"Звіти про помилки: {m['error_reports'] or '—'}")
    lines.append(f"Доставка: {m['delivery'] or '—'}")
    return "\n".join(lines)


async def analyze(db: Database, summarizer=None, hours: int = 24) -> str:
    """Текст звіту. Якщо модель недоступна — лише детерміноване резюме."""
    m = collect_metrics(db, hours=hours)
    issues = find_issues(m)
    body = ["🧠 <b>ІІ-аналіз системи</b>", "", render_metrics(m), ""]
    body.append("⚠️ <b>Проблеми</b>:\n" + "\n".join(f"• {i}" for i in issues) if issues else "✅ Проблем не знайдено.")
    if summarizer is not None and issues:
        prompt = render_metrics(m) + "\nПроблеми:\n" + "\n".join(issues)
        try:
            ai = await asyncio.wait_for(summarizer.classify(prompt, SYSTEM_PROMPT, max_tokens=500), timeout=90)
        except Exception:  # noqa: BLE001
            ai = ""
        if ai:
            body += ["", "🤖 <b>Висновок моделі</b>:", ai.strip()[:1500]]
    return "\n".join(body)


async def analyzer_loop(db: Database, bot, summarizer, super_admin_id: int,
                        interval_s: int = 6 * 3600) -> None:
    """Раз на interval_s шле звіт адмінам, лише якщо є проблеми (без спаму)."""
    await asyncio.sleep(300)
    while True:
        try:
            import event_lifecycle
            event_lifecycle.archive_old(db)
            m = collect_metrics(db)
            if find_issues(m):
                await notify_admins(bot, db, super_admin_id, await analyze(db, summarizer), parse_mode="html")
        except Exception:  # noqa: BLE001
            logger.warning("Аналізатор: збій", exc_info=True)
        await asyncio.sleep(interval_s)
