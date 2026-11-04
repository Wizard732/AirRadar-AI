"""Симуляція атаки через РЕАЛЬНИЙ конвеєр (без Telegram і без мережі): шахеди → крилаті ракети → балістика.

Що відбувається: вигадані пости каналів-джерел проходять main._process_message (фільтр, парсер, fusion, агрегатор,
рендер), у тимчасову БД; тестовий підписник «Київ» і гео-сторож у центрі Києва. Скрипт друкує, що пішло б у канал,
що отримав би підписник у ЛС і які попередження дав би гео-сторож. Нічого не надсилається назовні.
Запуск: .venv/bin/python scripts/simulate_attack.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app
from aggregator import AlertAggregator
from airradar.delivery import tg_delivery
from database import Database
from dedup import DedupCache
from kyiv_time import fmt as kyiv_fmt
import proximity

KYIV = (50.4501, 30.5234)
OUT: list[str] = []


class Pub:
    def __init__(self): self.n = 0
    async def send(self, text, *a, **k):
        self.n += 1; show("КАНАЛ", text); return {"chat": {"id": "c"}, "message_id": self.n}
    async def edit(self, *a, **k): return True
    async def send_photo(self, *a, **k): return True


class LLM:
    """Підставна «нейромережа»: на пости про вже сталий приліт відповідає JSON наслідків (як це робить MiniMax)."""
    async def summarize(self, text, *a, **k): return text
    async def classify(self, text, prompt, max_tokens=30):
        if "Відповідай ЛИШЕ одним JSON" in prompt:
            if "приліт" in text.lower() or "влучан" in text.lower():
                return '{"is_impact": true, "kind": "hit", "killed": null, "injured": 2, "children": null, "damage": ["житлові будинки", "пожежа"]}'
            return '{"is_impact": false, "kind": "none"}'
        return ""


def show(where: str, text: str) -> None:
    body = "\n".join("    │ " + ln for ln in text.splitlines())
    print(f"  ┌─ {where}\n{body}\n  └─")


async def main() -> None:
    f = tempfile.NamedTemporaryFile(delete=False); f.close()
    db = Database(f.name); pub = Pub(); dedup = DedupCache(ttl=60)
    app._DEDUP = type(app._DEDUP)()
    db.subscribe(1, "kyivska"); db.set_guard(1, True, 10.0); db.update_guard_location(1, KYIV[0], KYIV[1], True)

    async def deliver(bot, db_, key, uid, text, **kw):
        show(f"ЛС підписнику «Київщина» (user {uid})", text); return True
    tg_delivery.deliver = deliver

    async def flush(items): await app._publish_items(db, pub, object(), items)
    agg = AlertAggregator(0.05, flush, critical_needs_confirmation=True)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
    clock = [t0.timestamp()]
    app.time = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep)   # «годинник» симуляції: без фальшивих «опубліковано 40 хв тому»

    async def post(text, src, minutes):
        ts = t0 + timedelta(minutes=minutes)
        clock[0] = ts.timestamp() + 20
        ev = SimpleNamespace(message=SimpleNamespace(text=text, date=ts, photo=None, id=int(time.time() * 1000) % 10**9),
                             chat=SimpleNamespace(username=src))
        await app._process_message(ev, LLM(), pub, dedup, db, bot_client=object(), aggregator=agg)
        await asyncio.sleep(0.15)

    async def guard(label: str, minutes: float):
        sent: list[str] = []
        async def send(uid, text): sent.append(text)
        await proximity.evaluate_once(db, send, now=int((t0 + timedelta(minutes=minutes)).timestamp()))
        for s in sent: show(f"ГЕО-СТОРОЖ {label} (радіус 10 км, push зі звуком)", s)
        if not sent: print(f"  · гео-сторож {label}: мовчить (нічого поруч / слабкий сигнал)")

    def phase(title: str) -> None:
        print("\n" + "═" * 70 + f"\n{title}\n" + "═" * 70)

    phase("ФАЗА 1 · ШАХЕДИ: група з Чернігівщини йде на Київщину")
    await post("Чернігівщина: 6 БпЛА курсом на Ніжин", "src_a", 0)
    await post("Чернігівщина: БпЛА курсом на Бобровицю", "src_b", 6)
    await post("Київщина: БпЛА в районі Броварів", "src_a", 13)
    await post("Київщина: 3 БпЛА курсом на Бровари", "src_c", 14)
    await guard("t+14хв", 14)
    await post("Київщина: БпЛА курсом на Київ", "src_a", 18)
    await post("Київ: БпЛА курсом на місто", "src_b", 19)
    await guard("t+19хв", 19)

    phase("ФАЗА 2 · КРИЛАТІ РАКЕТИ")
    await post("Крилаті ракети курсом на Київ", "src_a", 24)
    await post("Київщина: крилаті ракети курсом на Київ", "src_b", 25)
    await guard("t+25хв", 25)

    phase("ФАЗА 3 · БАЛІСТИКА + ОФІЦІЙНА ТРИВОГА")
    await post("Балістика на Київ", "src_a", 29)
    await post("Загроза балістики для Києва", "src_b", 29.5)
    await post("🔴 Київська область — повітряна тривога", "src_c", 30)
    await guard("t+30хв", 30)

    phase("ФАЗА 4 · НАСЛІДКИ (нейромережа відрізняє від загрози) і ВІДБІЙ")
    await post("Київ: приліт по житловому будинку, 2 поранені, пожежа", "src_a", 34)
    import impact_log
    imp = impact_log.impacts_payload(db, now=int((t0 + timedelta(minutes=35)).timestamp()))
    print("  · журнал наслідків (те, що бачить вкладка «Наслідки»):", [(i["place"] or i["oblast"], i["injured"], i["damage"]) for i in imp["items"]])
    await post("🟢 Київська область — відбій повітряної тривоги", "src_c", 40)
    await agg.aclose()
    os.unlink(f.name)


asyncio.run(main())
