"""sirens_poll.py — опитування офіційних сирен alerts.in.ua (винесено з main.py без зміни поведінки)."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

from database import Database

logger = logging.getLogger("main")   # те саме ім'я логера, що й раніше (у main.py)

def _main():
    """Лінивий імпорт main (уникає циклічного імпорту; main імпортує цей модуль)."""
    from airradar.app_ref import main_module
    return main_module()


SIRENS_POLL_INTERVAL = 60


async def _sirens_loop(db: Database, token: str, http_session=None,
                       interval: int = SIRENS_POLL_INTERVAL, publisher=None, bot_client=None) -> None:
    """Фоновый опрос официальных сирен alerts.in.ua → таблица siren_states.

    Данные нужны /status (свежесть) и сверке наших прогнозов с официальными
    тревогами. Ошибка сети/токена не роняет цикл и НЕ трогает состояния
    (иначе краткосрочный сбой «выключил» бы все сирены): устаревание видно
    по updated_ts через siren_states_fresh.
    """
    session = http_session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    )
    own_session = http_session is None
    primed = False
    try:
        while True:
            incoming = await _sirens_fetch(session, token)
            if incoming is not None:
                try:
                    db.alert_sync(_LAST_ALERTS, int(time.time()))
                except Exception:  # noqa: BLE001 — журнал не критичний
                    logger.debug("alert_sync не вдався", exc_info=True)
                try:
                    # Райони («raion:…») — лише для карти: окремо від сповіщень по областях.
                    raions = {k for k in incoming if k.startswith(("raion", "threat:"))}
                    incoming = incoming - raions
                    cur_all = set(db.siren_states_active())
                    for k in raions:
                        db.set_siren_state(k, True)
                    for k in {k for k in cur_all if k.startswith(("raion", "threat:"))} - raions:
                        db.set_siren_state(k, False)
                    current = {k for k in cur_all if not k.startswith(("raion", "threat:"))}
                    async def _notify(regs, msg):
                        if bot_client is not None:
                            await _main()._notify_subscribers(bot_client, db, regs, msg)
                            if msg.startswith("🚨"):
                                import family_status
                                await family_status.send_alarm_prompt(db, bot_client, regs)
                        if not msg.startswith("🚨"):
                            import event_lifecycle
                            event_lifecycle.resolve_active(db)   # офіційний відбій закриває активні події
                        import push_fcm
                        import webpush_push
                        await webpush_push.push_regions(db, regs, "🚨 ПОВІТРЯНА ТРИВОГА" if msg.startswith("🚨") else "🟢 Відбій", msg)
                        import hashlib as _h
                        await push_fcm.push_regions(db, regs, "🚨 ПОВІТРЯНА ТРИВОГА" if msg.startswith("🚨") else "🟢 Відбій", msg,
                                                    "alarm" if msg.startswith("🚨") else "info",
                                                    ack_key=_h.md5(msg.encode()).hexdigest()[:16] if msg.startswith("🚨") else "")
                    import alarm_notices
                    for slug in sorted(incoming - current):
                        db.set_siren_state(slug, True)
                        logger.info("Сирена ВКЛ (официально): %s", slug)
                        if publisher is not None and primed:   # первый опрос после старта не засыпает канал
                            await alarm_notices.apply(db, publisher, _notify, "start", [slug], int(time.time()))
                    for slug in sorted(current - incoming):
                        db.set_siren_state(slug, False)
                        logger.info("Сирена ВЫКЛ (официально): %s", slug)
                        if publisher is not None:
                            await alarm_notices.apply(db, publisher, _notify, "end", [slug], int(time.time()))
                    for slug in sorted(incoming & current):
                        db.set_siren_state(slug, True)  # обновить updated_ts
                    primed = True
                except Exception:  # noqa: BLE001 — БД не роняет цикл
                    logger.exception("Сирены: не удалось сохранить состояния")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        if own_session:
            await session.close()
        raise


async def _sirens_fetch(session, token: str) -> set[str] | None:
    """Один опрос alerts.in.ua → множество слагов; None = опрос не удался."""
    from sirens import active_alert_slugs, fetch_active_alerts
    payload = await fetch_active_alerts(session, token)
    if payload is None:
        return None
    from sirens import active_extra_keys, active_raion_keys, alert_details
    global _LAST_ALERTS
    _LAST_ALERTS = alert_details(payload)
    for _a in _LAST_ALERTS:   # ISO → epoch для журналу
        try:
            from datetime import datetime as _dt
            _a["started_ts"] = int(_dt.fromisoformat(_a["since"].replace("Z", "+00:00")).timestamp()) if _a.get("since") else 0
        except ValueError:
            _a["started_ts"] = 0
    try:   # деталі для листа області на карті: файл поруч із БД, читає export_radar / health_server
        import json as _json, tempfile as _tmp
        _p = os.path.join(os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "airradar.db"))), "siren_detail.json")
        with _tmp.NamedTemporaryFile("w", dir=os.path.dirname(_p), delete=False, encoding="utf-8") as _f:
            _json.dump({"ts": int(time.time()), "alerts": alert_details(payload)}, _f, ensure_ascii=False)
        os.replace(_f.name, _p)
    except Exception:  # noqa: BLE001 — деталі не критичні
        logger.debug("siren_detail не записано", exc_info=True)
    return active_alert_slugs(payload) | active_raion_keys(payload) | active_extra_keys(payload)
