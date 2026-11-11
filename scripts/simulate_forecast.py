"""Симуляція прогнозів «коли відбій» і «коли нова тривога» на РЕАЛЬНОМУ коді (без Telegram і мережі).

Тимчасова БД заповнюється ВИГАДАНОЮ історією Київщини (6 ночей × 2 хвилі: останній політ → відбій ~25 хв, пауза до нової
тривоги ~55 хв). Далі «годинник» симуляції проходить поточну ніч, а фонові перевірки бота (main._check_standdown /
_check_pre_wave) надсилають повідомлення підписнику й у канал. Скрипт друкує, що саме він отримав.
Запуск: .venv/bin/python scripts/simulate_forecast.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app
import wave_forecast
from airradar.delivery import tg_delivery
from database import Database
from kyiv_time import fmt as kyiv_fmt
from regions import region_name

REGION = "kyivska"
MIN = 60


def show(where: str, text: str) -> None:
    body = "\n".join("    │ " + ln for ln in text.splitlines())
    print(f"  ┌─ {where}\n{body}\n  └─")


class Pub:
    async def send(self, text, *a, **k):
        show("КАНАЛ", text); return {"chat": {"id": "c"}, "message_id": 1}


async def main() -> None:
    f = tempfile.NamedTemporaryFile(delete=False); f.close()
    db = Database(f.name); db.subscribe(1, REGION)
    base = int(time.time()) - 8 * 86400
    # --- вигадана історія: 6 ночей, у кожній 2 хвилі
    for night in range(6):
        t = base + night * 86400 + 2 * 3600
        for wave in range(2):
            start = t + wave * 115 * MIN
            flight = start + 20 * MIN + (night % 3) * 2 * MIN
            end = flight + (23 + (night % 4) * 2) * MIN
            db.alert_start(REGION, event_ts=start)
            db.add_event(event_ts=start - 5 * MIN, weapon_class="shahed", stage="launch", region=REGION, text="історія", source="seed")
            db.add_event(event_ts=flight, weapon_class="shahed", stage="movement", region=REGION, text="історія", source="seed")
            db.alert_end(REGION, event_ts=end)
    print("Історія: 12 завершених хвиль Київщини за 6 ночей (вигадані).\n")

    clock = [0]
    wave_forecast.time = SimpleNamespace(time=lambda: clock[0])
    app.time = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep)

    async def deliver(bot, db_, key, uid, text, **kw):
        show(f"ЛС підписнику «{region_name(REGION)}»", text); return True
    tg_delivery.deliver = deliver

    pub = Pub(); now0 = int(time.time()) - 3 * 3600
    start = now0
    db.alert_start(REGION, event_ts=start)
    db.add_event(event_ts=start + 4 * MIN, weapon_class="shahed", stage="launch", region=REGION, text="Київщина: БпЛА курсом на Київ", source="src_a")
    flight = start + 30 * MIN
    db.add_event(event_ts=flight, weapon_class="shahed", stage="movement", region=REGION, text="Київщина: БпЛА курсом на Київ", source="src_a")

    def phase(t: str): print("\n" + "═" * 70 + f"\n{t}\n" + "═" * 70)

    phase("1 · ТРИВОГА ТРИВАЄ, останній політ був 30 хв тому — бот перевіряє «коли відбій»")
    for dt in (5, 14, 19):
        clock[0] = flight + dt * MIN
        print(f"  [час: {kyiv_fmt(clock[0])}, з останнього поста про політ {dt} хв]")
        sent = await app._check_standdown(object(), db, pub)
        if not sent: print("  · мовчить (ще зарано або вже повідомляв)")

    phase("2 · ВІДБІЙ → прогноз «коли нова тривога»")
    end = flight + 22 * MIN
    db.alert_end(REGION, event_ts=end)
    for dt in (30, 62, 75):
        clock[0] = end + dt * MIN
        print(f"  [час: {kyiv_fmt(clock[0])}, з відбою {dt} хв]")
        sent = await app._check_pre_wave(object(), db, pub)
        if not sent: print("  · мовчить (поки не підійшов типовий час нової хвилі)")

    phase("3 · КНОПКА БОТА «⏳ Коли відбій / тривога» (/wave) — що бачить користувач")
    for label, ts in (("ПІД ЧАС тривоги (за 10 хв після останнього поста про політ)", None), ("ПІСЛЯ відбою (через 30 хв)", end + 30 * MIN)):
        if ts is None:
            db._conn.execute("UPDATE alerts SET ended_ts=NULL WHERE region=? AND started_ts=?", (REGION, start)); db._conn.commit()
            clock[0] = flight + 10 * MIN
        else:
            db._conn.execute("UPDATE alerts SET ended_ts=? WHERE region=? AND started_ts=?", (end, REGION, start)); db._conn.commit()
            clock[0] = ts
        show(f"ЕКРАН БОТА · {label}", wave_forecast.format_wave_forecast(db, REGION, region_name(REGION)).replace("<b>", "").replace("</b>", ""))
    os.unlink(f.name)


asyncio.run(main())
