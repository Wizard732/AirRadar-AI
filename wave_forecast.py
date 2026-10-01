"""wave_forecast.py — статистика волн: «коли відбій» і «коли нова тривога».

Модель (на согласованим с пользователем принципе): НЕ гадать физически,
а считать статистику региона по завершённым эпизодам тревоги:

  • ОТБОЙ: медиана «последний пост о полёте (пуск/движение) → отбой».
    Пример: по статистике последний Shahed сбивают за ~20 мин — значит,
    если последнее сообщение о полёте было 5 минут назад, відбій
    орієнтовно через ~15 хв.

  • НОВАЯ ВОЛНА: медиана паузы «отбой → старт следующей тревоги».
    Пример: Shahed с Черниговщины идут волнами — после отбоя следующая
    тривога в среднем через ~40 мин.

Каждая оценка помечается как статистическая (правило проекта: прогнозы
только с пометкой «орієнтовно/статистична оцінка»). Пока мало эпизодов
(< MIN_SAMPLES) — честно возвращаем «недостаточно данных», не выдумываем.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from typing import Any

from database import Database

# Глубина истории эпизодов и максимум выборки.
EPISODE_WINDOW_DAYS = 45
MAX_EPISODES = 10

# Стадии threat_events, означающие «цель в воздухе».
FLIGHT_STAGES = ("launch", "movement")

# Разрыв «последний полётный пост → отбой» дальше 3 ч — эпизод неинформативен
# (отбой по другой причине), в медиану его не берём.
STANDDOWN_MAX_GAP_S = 3 * 3600

# Полётный пост может появиться ЗАДОЛГО до старта официальной тревоги
# (мониторинг быстрее сирен). Ищем его максимум за 15 мин до старта,
# но не заходя в предыдущий эпизод.
FLIGHT_PRE_ALERT_SLACK_S = 15 * 60

# Минимум эпизодов/пар для честной медианы. Меньше — «недостаточно данных».
MIN_SAMPLES = 3

# Пауза «отбой → новая тревога»: короче 15 мин — тот же эпизод (двойной
# старт), длиннее 8 ч — «другая ночь», не тренд этой волны.
NEXT_WAVE_MIN_GAP_S = 15 * 60
NEXT_WAVE_MAX_GAP_S = 8 * 3600

# Проактивное «можлива нова тривога» после отбоя: сообщаем за 10 мин до
# расчётного старта волны (LEAD) и не позже 20 мин после него (LAG —
# страховка на случай паузы бота/офлайна). Вне окна — молчим.
PRE_WAVE_LEAD_S = 10 * 60
PRE_WAVE_LAG_S = 20 * 60

# Профиль «часы волн»: круговое окно в часах суток и минимальная выборка.
PROFILE_WINDOW_HOURS = 6
PROFILE_MIN_EPISODES = 5


def _quantile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = int(position), int(-(-position // 1))  # ceil
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _kyiv_hour_of(ts: int) -> int:
    """Час суток в Киеве для unix-ts (общий хелпер kyiv_time)."""
    from kyiv_time import kyiv_hour
    return kyiv_hour(ts)


def _fmt_duration(minutes: int) -> str:
    """Человекочитаемая длительность: 100 → «1год40хв», 45 → «45хв»."""
    minutes = max(0, int(minutes))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}год{mins:02d}хв"
    if hours:
        return f"{hours}год"
    return f"{mins}хв"


def _episodes(conn, region: str, since: int) -> list:
    """Завершённые эпизоды тревоги региона: свежие первыми."""
    return conn.execute(
        "SELECT started_ts, ended_ts FROM alerts "
        "WHERE region=? AND ended_ts IS NOT NULL AND ended_ts>=? "
        "ORDER BY ended_ts DESC LIMIT ?",
        (region, since, MAX_EPISODES),
    ).fetchall()


def _standdown_gaps(conn, region: str, since: int) -> list[float]:
    """Гэпы «последний полётный пост → отбой» по завершённым эпизодам.

    Общая база для standdown_estimate (медиана) и standdown_hit_rate
    (честный hit-rate прогноза). Эпизод без полётного поста или с гэпом
    больше STANDDOWN_MAX_GAP_S (отбой по другой причине) не учитывается.
    """
    gaps: list[float] = []
    prev_end = None
    for ep in sorted(_episodes(conn, region, since), key=lambda r: r["ended_ts"]):
        lower = ep["started_ts"] - FLIGHT_PRE_ALERT_SLACK_S
        if prev_end is not None:
            lower = max(lower, prev_end + 1)  # не захватываем прошлый эпизод
        flight = _last_flight_ts(conn, region, lower, ep["ended_ts"])
        prev_end = ep["ended_ts"]
        if flight is None:
            continue
        gap = ep["ended_ts"] - flight
        if 0 < gap <= STANDDOWN_MAX_GAP_S:
            gaps.append(float(gap))
    return gaps


def _last_flight_ts(conn, region: str, after_ts: int, before_ts: int) -> int | None:
    """Ts последнего «полётного» события региона в окне [after_ts, before_ts]."""
    ph = ",".join("?" * len(FLIGHT_STAGES))
    row = conn.execute(
        f"SELECT MAX(event_ts) m FROM threat_events "
        f"WHERE region=? AND stage IN ({ph}) AND event_ts>=? AND event_ts<=?",
        (region, *FLIGHT_STAGES, after_ts, before_ts),
    ).fetchone()
    return int(row["m"]) if row and row["m"] else None


def standdown_estimate(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Медиана «последнее сообщение о полёте → отбой» по завершённым эпизодам.

    Это база прогноза: сколько минут обычно проходит от последнего поста
    о цели в воздухе до официального отбоя в регионе.
    """
    now = int(now if now is not None else time.time())
    since = now - EPISODE_WINDOW_DAYS * 86400
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"available": False, "samples": 0}
        gaps = _standdown_gaps(conn, region, since)
        if len(gaps) < MIN_SAMPLES:
            return {"available": False, "samples": len(gaps)}
        return {
            "available": True,
            "samples": len(gaps),
            "median_seconds": statistics.median(gaps),
            "p20_seconds": _quantile(gaps, 0.2),
            "p80_seconds": _quantile(gaps, 0.8),
        }
    except sqlite3.Error:
        return {"available": False, "samples": 0}


def standdown_accuracy(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Честный hit-rate прогноза отбоя: «відбій у межах медіани: N/M».

    Ретроспективная проверка leave-one-out: для каждого эпизода медиана
    считается только по ОСТАЛЬНЫМ эпизодам (без подглядывания в будущее),
    затем проверяем, пришёл ли фактический отбой не позже этой медианы.
    Показываем метрику с MIN_SAMPLES+1 эпизодов — при меньшей выборке
    медиана «по остальным» слишком шумная.
    """
    now = int(now if now is not None else time.time())
    since = now - EPISODE_WINDOW_DAYS * 86400
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"available": False, "hits": 0, "total": 0}
        gaps = _standdown_gaps(conn, region, since)
        n = len(gaps)
        if n < MIN_SAMPLES + 1:
            return {"available": False, "hits": 0, "total": n}
        hits = 0
        for i in range(n):
            others = gaps[:i] + gaps[i + 1:]
            if gaps[i] <= statistics.median(others):
                hits += 1
        return {"available": True, "hits": hits, "total": n}
    except sqlite3.Error:
        return {"available": False, "hits": 0, "total": 0}


def public_accuracy(db: Database, regions: list[str] | None = None, *, now: int | None = None) -> dict[str, Any]:
    """Публичная точность прогнозов: «X% відбоїв у межах медіани».

    Сумма leave-one-out hit-rate (standdown_accuracy) по регионам с
    эпизодами за окно. Регионы без достаточной выборки честно не
    учитываются — метрика не завышается. Используется в статистике бота
    («🎯 Точність прогнозів») как открытая метрика доверия.
    """
    now = int(now if now is not None else time.time())
    if regions is None:
        try:
            conn = db._conn  # type: ignore[attr-defined]
            if conn is None:
                return {"available": False, "hits": 0, "total": 0, "regions": 0}
            since = now - EPISODE_WINDOW_DAYS * 86400
            rows = conn.execute(
                "SELECT DISTINCT region FROM alerts WHERE started_ts>=?", (since,)
            ).fetchall()
            regions = [r["region"] for r in rows]
        except sqlite3.Error:
            return {"available": False, "hits": 0, "total": 0, "regions": 0}
    hits = total = 0
    counted = 0
    for slug in regions:
        acc = standdown_accuracy(db, slug, now=now)
        hits += int(acc.get("hits", 0))
        total += int(acc.get("total", 0))
        if acc.get("available"):
            counted += 1
    return {"available": total > 0, "hits": hits, "total": total, "regions": counted}


def wave_hour_profile(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Часы суток (Київ), когда тревоги региона начинаются чаще всего.

    Ищем круговое окно PROFILE_WINDOW_HOURS часов с максимумом стартов
    тревоги. Показываем только если эпизодов ≥ PROFILE_MIN_EPISODES и
    окно покрывает ≥60% из них — иначе честно «профиля нет».
    """
    now = int(now if now is not None else time.time())
    since = now - EPISODE_WINDOW_DAYS * 86400
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"available": False, "samples": 0}
        rows = conn.execute(
            "SELECT started_ts FROM alerts WHERE region=? AND started_ts>=? LIMIT 200",
            (region, since),
        ).fetchall()
        if len(rows) < PROFILE_MIN_EPISODES:
            return {"available": False, "samples": len(rows)}
        hours = [_kyiv_hour_of(int(r["started_ts"])) for r in rows]
        best_start, best_cnt = 0, -1
        for start in range(24):
            window = {(start + i) % 24 for i in range(PROFILE_WINDOW_HOURS)}
            cnt = sum(1 for h in hours if h in window)
            if cnt > best_cnt:
                best_start, best_cnt = start, cnt
        if best_cnt * 5 < len(hours) * 3:  # <60% концентрации — профиля нет
            return {"available": False, "samples": len(hours)}
        return {
            "available": True,
            "samples": len(hours),
            "from_hour": best_start,
            "to_hour": (best_start + PROFILE_WINDOW_HOURS) % 24,
        }
    except sqlite3.Error:
        return {"available": False, "samples": 0}


def episode_progress(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Статус текущего эпизода: сколько длится и типичная длительность.

    «Хвиля триває 1год20хв, типова тривалість ~1год40хв» — прогресс даёт
    понимание, насколько эпизод затянулся относительно истории региона.
    """
    now = int(now if now is not None else time.time())
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"active": False}
        row = conn.execute(
            "SELECT started_ts FROM alerts WHERE region=? AND ended_ts IS NULL "
            "ORDER BY started_ts DESC LIMIT 1",
            (region,),
        ).fetchone()
        if row is None:
            return {"active": False}
        elapsed_s = max(0, now - int(row["started_ts"]))
        since = now - EPISODE_WINDOW_DAYS * 86400
        durs = [
            float(r["d"])
            for r in conn.execute(
                "SELECT ended_ts - started_ts AS d FROM alerts "
                "WHERE region=? AND ended_ts IS NOT NULL AND started_ts>=? "
                "AND ended_ts > started_ts",
                (region, since),
            ).fetchall()
            if r["d"] and r["d"] > 0
        ]
        median_min = (
            round(statistics.median(durs) / 60) if len(durs) >= MIN_SAMPLES else None
        )
        return {
            "active": True,
            "elapsed_min": elapsed_s // 60,
            "median_min": median_min,
            "over_median": median_min is not None and elapsed_s > median_min * 60,
        }
    except sqlite3.Error:
        return {"active": False}


def next_wave_estimate(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Медиана паузы «отбой → старт следующей тревоги» в регионе.

    Основная метрика — интервал между завершением эпизода и стартом
    следующего. Если прямых пар мало, фолбэк — интервалы старт→старт.
    """
    now = int(now if now is not None else time.time())
    since = now - EPISODE_WINDOW_DAYS * 86400
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"available": False, "samples": 0}
        episodes = sorted(
            _episodes(conn, region, since), key=lambda r: r["started_ts"]
        )
        gaps: list[float] = []
        prev_end = None
        for ep in episodes:
            if prev_end is not None:
                gap = ep["started_ts"] - prev_end
                if NEXT_WAVE_MIN_GAP_S <= gap <= NEXT_WAVE_MAX_GAP_S:
                    gaps.append(float(gap))
            prev_end = ep["ended_ts"]
        # Фолбэк: старт→старт (когда ended_ts редко писался исторически).
        if len(gaps) < MIN_SAMPLES and len(episodes) >= MIN_SAMPLES + 1:
            starts = [ep["started_ts"] for ep in episodes]
            gaps = [
                float(b - a)
                for a, b in zip(starts, starts[1:])
                if NEXT_WAVE_MIN_GAP_S <= b - a <= NEXT_WAVE_MAX_GAP_S
            ]
        if len(gaps) < MIN_SAMPLES:
            return {"available": False, "samples": len(gaps)}
        return {
            "available": True,
            "samples": len(gaps),
            "median_seconds": statistics.median(gaps),
            "p20_seconds": _quantile(gaps, 0.2),
            "p80_seconds": _quantile(gaps, 0.8),
        }
    except sqlite3.Error:
        return {"available": False, "samples": 0}


def live_standdown(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Прогноз отбоя «прямо сейчас»: цель в воздухе → сколько осталось.

    remaining_min = медиана(последний полёт → отбой) − (сейчас − последний
    полётный пост). Если медиана уже исчерпана — «відбій очікується будь-якої
    хвилини» (remaining_min=0). Нет недавнего полёта — прогноз не выдаём.
    """
    est = standdown_estimate(db, region, now=now)
    if not est["available"]:
        return est
    now = int(now if now is not None else time.time())
    try:
        conn = db._conn  # type: ignore[attr-defined]
        last = _last_flight_ts(conn, region, now - STANDDOWN_MAX_GAP_S, now)
    except sqlite3.Error:
        return {"available": False, "samples": est["samples"]}
    if last is None:
        return {"available": False, "samples": est["samples"], "no_active_flight": True}
    remaining_s = est["median_seconds"] - (now - last)
    return {
        "available": True,
        "samples": est["samples"],
        "median_minutes": round(est["median_seconds"] / 60),
        "elapsed_minutes": round((now - last) / 60),
        "remaining_min": max(0, round(remaining_s / 60)),
        "any_minute_now": remaining_s <= 0,
    }


# Проактивное «відбій орієнтовно за ~N хв»: сообщаем один раз на эпизод
# полёта, когда расчётный остаток до отбоя опускается до этого порога.
STANDDOWN_NOTICE_LEAD_MIN = 25

# Один countdown на эпизод тревоги. Повтор внутри того же эпизода — только
# если оценка остатка изменилась на ≥10 минут (новая группа целей заметно
# перезапустила таймер). Дедуп по точному flight_ts остаётся: пока новых
# постов нет (тихий хвост перед отбоем), повторов не бывает вовсе — именно
# так лечился спам «⏳ відбій орієнтовно» из логов 2026-09-30.
STANDDOWN_NOTICE_RESEND_DELTA_MIN = 10

# Кулдаун на регион — страховка последней линии: даже при законном повторе
# (Δ≥10 мин) новый countdown выходит не чаще раза в 20 минут.
STANDDOWN_NOTICE_COOLDOWN_S = 20 * 60


def standdown_notice(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Нужно ли прямо сейчас написать в чат «відбій орієнтовно за ~N хв».

    Логика: в регионе активная тревога (её started_ts — ключ эпизода) +
    live-статистика доступна (≥ MIN_SAMPLES эпизодов) + цель в воздухе
    (полётный пост в пределах STANDDOWN_MAX_GAP_S) + остаток до отбоя по
    медиане опустился до STANDDOWN_NOTICE_LEAD_MIN. Дедуп по слоям:

    1. точный полётный пост уже отработал → already_sent (тихий хвост:
       постов нет — countdown не повторяется);
    2. в этом эпизоде тревоги уже отправляли, и оценка изменилась меньше
       чем на STANDDOWN_NOTICE_RESEND_DELTA_MIN → already_sent;
    3. по региону отправляли в последние STANDDOWN_NOTICE_COOLDOWN_S →
       cooldown (страховка от осцилляций оценки).
    """
    now = int(now if now is not None else time.time())
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"notify": False, "reason": "no_db"}
        row = conn.execute(
            "SELECT started_ts FROM alerts WHERE region=? AND ended_ts IS NULL "
            "ORDER BY started_ts DESC LIMIT 1",
            (region,),
        ).fetchone()
        if row is None:
            return {"notify": False, "reason": "no_alert"}
        alert_started_ts = int(row["started_ts"])
        live = live_standdown(db, region, now=now)
        # no_active_flight приходит с available=False — проверяем его ДО
        # общей ветки no_stats, иначе «нет полёта» навсегда маскируется под
        # «недостаточно данных» (регрессия test_silent_when_no_recent_flight).
        if live.get("no_active_flight"):
            return {"notify": False, "reason": "no_flight", "samples": live.get("samples", 0)}
        if not live["available"]:
            return {"notify": False, "reason": "no_stats", "samples": live.get("samples", 0)}
        last = _last_flight_ts(conn, region, now - STANDDOWN_MAX_GAP_S, now)
        if last is None:
            return {"notify": False, "reason": "no_flight"}
        if live["remaining_min"] > STANDDOWN_NOTICE_LEAD_MIN:
            return {"notify": False, "reason": "too_early"}
        # Слой 1: этот полётный пост уже отработал. Пока новых постов нет,
        # никаких повторов — даже если оценка «тает» со временем.
        if db.standdown_notice_sent(region, last):
            return {"notify": False, "reason": "already_sent"}
        # Слой 2: один countdown на эпизод тревоги; повтор — только при
        # изменении оценки на ≥10 минут.
        prev = db.standdown_episode_notice(region, alert_started_ts)
        if prev is not None and abs(int(live["remaining_min"]) - prev["remaining_min"]) < STANDDOWN_NOTICE_RESEND_DELTA_MIN:
            return {"notify": False, "reason": "already_sent"}
        # Слой 3: кулдаун-страховка.
        if db.standdown_notice_sent_recent(region, STANDDOWN_NOTICE_COOLDOWN_S):
            return {"notify": False, "reason": "cooldown"}
        return {
            "notify": True,
            "alert_started_ts": alert_started_ts,
            "flight_ts": int(last),
            "remaining_min": int(live["remaining_min"]),
            "any_minute_now": bool(live["any_minute_now"]),
            "median_minutes": int(live["median_minutes"]),
            "elapsed_minutes": int(live["elapsed_minutes"]),
            "samples": int(live["samples"]),
        }
    except sqlite3.Error:
        return {"notify": False, "reason": "db_error"}


def format_standdown_notice(region: str, info: dict[str, Any], name: str = "") -> str:
    """Текст countdown-сообщения «відбій орієнтовно за ~N хв» + почему."""
    from regions import region_name

    title = name or region_name(region)
    mins = int(info.get("remaining_min", 0))
    head = (
        f"⏳ {title} — відбій орієнтовно будь-якої хвилини"
        if info.get("any_minute_now")
        else f"⏳ {title} — відбій орієнтовно за ~{mins} хв"
    )
    reason = (
        f"Чому: останнє повідомлення про ціль у повітрі було ~"
        f"{int(info.get('elapsed_minutes', 0))} хв тому, а за статистикою "
        f"регіону ({int(info.get('samples', 0))} епізодів) від останнього "
        f"поста про політ до відбою проходить ~"
        f"{int(info.get('median_minutes', 0))} хв."
    )
    return (
        f"{head}\n\n{reason}\n\n"
        "⚠️ Статистична оцінка, не гарантія. Сирени — головне джерело."
    )


def region_forecast(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Компактный прогноз для карты/API: только числа, без текста.

    {"standdown_min": int|None, "standdown_live": bool, "next_wave_min": int,
     "standdown_hits": int, "standdown_total": int, "wave_hours": {"from","to"}}
    — пустой dict, если по региону нет статистики (карта просто не
    показывает блок). standdown_live=True: число — live-остаток до отбоя
    (countdown на карте); False: типичный гэп «последний полёт → отбой».
    """
    out: dict[str, Any] = {}
    live = live_standdown(db, region, now=now)
    if live["available"]:
        out["standdown_min"] = max(0, int(live["remaining_min"]))
        out["standdown_live"] = True
    else:
        base = standdown_estimate(db, region, now=now)
        if base["available"]:
            out["standdown_min"] = int(base["median_seconds"] / 60)
            out["standdown_live"] = False
    wave = next_wave_estimate(db, region, now=now)
    if wave["available"]:
        out["next_wave_min"] = int(wave["median_seconds"] / 60)
    try:
        acc = standdown_accuracy(db, region, now=now)
        if acc["available"]:
            out["standdown_hits"] = int(acc["hits"])
            out["standdown_total"] = int(acc["total"])
    except Exception:  # noqa: BLE001 — карта важнее метрики
        pass
    try:
        profile = wave_hour_profile(db, region, now=now)
        if profile["available"]:
            out["wave_hours"] = {
                "from": int(profile["from_hour"]),
                "to": int(profile["to_hour"]),
            }
    except Exception:  # noqa: BLE001
        pass
    return out


def pre_wave_notice(db: Database, region: str, *, now: int | None = None) -> dict[str, Any]:
    """Нужно ли прямо сейчас предупредить подписчиков о возможной новой волне.

    Логика (KISS): регион без активной тревоги + статистика пауз доступна
    (≥ MIN_SAMPLES) + с последнего отбоя прошло время в окне
    [медиана − LEAD, медиана + LAG] + по этому эпизоду уведомление ещё
    не отправлялось → «notify: True» с медианой для текста.
    """
    now = int(now if now is not None else time.time())
    try:
        conn = db._conn  # type: ignore[attr-defined]
        if conn is None:
            return {"notify": False, "reason": "no_db"}
        active = conn.execute(
            "SELECT 1 FROM alerts WHERE region=? AND ended_ts IS NULL LIMIT 1",
            (region,),
        ).fetchone()
        if active:
            return {"notify": False, "reason": "alert_active"}
        wave = next_wave_estimate(db, region, now=now)
        if not wave["available"]:
            return {"notify": False, "reason": "no_stats", "samples": wave["samples"]}
        row = conn.execute(
            "SELECT MAX(ended_ts) AS e FROM alerts WHERE region=? AND ended_ts IS NOT NULL",
            (region,),
        ).fetchone()
        last_end = int(row["e"]) if row and row["e"] else None
        if last_end is None:
            return {"notify": False, "reason": "no_episode"}
        if db.wave_notice_sent(region, last_end):
            return {"notify": False, "reason": "already_sent"}
        since_end = now - last_end
        target = wave["median_seconds"]
        if since_end < target - PRE_WAVE_LEAD_S or since_end > target + PRE_WAVE_LAG_S:
            return {"notify": False, "reason": "outside_window"}
        return {
            "notify": True,
            "episode_end_ts": last_end,
            "median_minutes": round(target / 60),
            "samples": wave["samples"],
        }
    except sqlite3.Error:
        return {"notify": False, "reason": "db_error"}


def format_pre_wave_notice(region: str, info: dict[str, Any], name: str = "") -> str:
    """Текст проактивного предупреждения «можлива нова тривога» (plain)."""
    from regions import region_name

    title = name or region_name(region)
    mins = int(info.get("median_minutes", 0))
    samples = int(info.get("samples", 0))
    return (
        f"⚠️ {title} — можлива нова тривога\n\n"
        f"За статистикою регіону ({samples} пауз між хвилями) наступна "
        f"тривога ймовірна приблизно зараз: медіана «відбій → нова тривога» "
        f"— ~{mins} хв.\n\n"
        "⚠️ Статистична оцінка, не гарантія. Сирени — головне джерело."
    )


def format_wave_forecast(db: Database, region: str, name: str) -> str:
    """Человекочитаемый прогноз волн для кнопки бота (пометка «статистика»)."""
    from regions import region_name

    title = name or region_name(region)
    lines: list[str] = [f"🔮 <b>{title}</b> — прогноз хвиль (статистика)\n"]

    # Прогресс текущего эпизода: «хвиля триває X, типова тривалість Y».
    try:
        ep = episode_progress(db, region)
        if ep["active"]:
            line = f"⏱ Хвиля триває <b>~{_fmt_duration(ep['elapsed_min'])}</b>"
            if ep["median_min"]:
                if ep["over_median"]:
                    line += (
                        f" — уже довше типового (~{_fmt_duration(ep['median_min'])}): "
                        f"ймовірно, це не остання ціль."
                    )
                else:
                    line += f", типова тривалість епізоду ~{_fmt_duration(ep['median_min'])}."
            lines.append(line)
    except Exception:  # noqa: BLE001 — прогресс не роняет прогноз
        pass

    live = live_standdown(db, region)
    base = standdown_estimate(db, region)
    if live["available"]:
        if live["any_minute_now"]:
            lines.append(
                f"⏳ Ціль у повітрі вже ~{live['elapsed_minutes']} хв: відбій "
                f"орієнтовно <b>будь-якої хвилини</b>."
            )
        else:
            lines.append(
                f"⏳ Якщо це остання ціль: відбій орієнтовно за "
                f"<b>~{live['remaining_min']} хв</b> "
                f"(медіана: останній політ → відбій ~{live['median_minutes']} хв)."
            )
    elif base["available"]:
        mins = round(base["median_seconds"] / 60)
        lines.append(
            f"⏳ Типовий час «остання ціль у повітрі → відбій»: <b>~{mins} хв</b> "
            f"(статистика {base['samples']} епізодів)."
        )

    # Честная метрика точности: сколько раз фактический отбой укладывался
    # в медиану, посчитанную без этого эпизода (leave-one-out).
    try:
        acc = standdown_accuracy(db, region)
        if acc["available"]:
            lines.append(
                f"🎯 Точність: відбій у межах медіани у "
                f"<b>{acc['hits']}/{acc['total']}</b> епізодів."
            )
    except Exception:  # noqa: BLE001 — метрика не роняет прогноз
        pass

    # Профиль «часы волн»: когда в регионе обычно начинаются тревоги.
    try:
        profile = wave_hour_profile(db, region)
        if profile["available"]:
            fh, th = profile["from_hour"], profile["to_hour"]
            lines.append(
                f"🌙 Тривоги тут зазвичай починаються близько "
                f"<b>{fh:02d}:00–{th:02d}:00</b> за Києвом "
                f"(статистика {profile['samples']} епізодів)."
            )
    except Exception:  # noqa: BLE001
        pass

    wave = next_wave_estimate(db, region)
    if wave["available"]:
        mins = round(wave["median_seconds"] / 60)
        lines.append(
            f"🔁 Наступна тривога після відбою: імовірна через <b>~{mins} хв</b> "
            f"(статистика {wave['samples']} пауз між хвилями)."
        )

    if len(lines) == 1:
        samples = max(base.get("samples", 0), wave.get("samples", 0))
        return (
            f"🔮 <b>{title}</b> — прогноз хвиль\n\n"
            f"Недостатньо даних для статистики регіону "
            f"(потрібно {MIN_SAMPLES}+ завершених епізодів тривоги, зараз: {samples}).\n"
            f"Бот накопичує історію — оцінка з'явиться пізніше.\n"
        )

    lines.append("\n⚠️ Статистична оцінка, не гарантія. Спираюйся на офіційні сирени.")
    return "\n".join(lines)


def accuracy_post_text(db: Database, *, now: int | None = None) -> str:
    """Недельный пост «🎯 Точність прогнозів» для канала (plain, самодостаточный).

    Открытая метрика доверия: что бот реально угадывает. Считаем по окну
    эпизодов (EPISODE_WINDOW_DAYS):
      • % отбоев в межах медианы (leave-one-out, без подглядывания в будущее);
      • опережение официальной сирены (siren_lead_stats за 7 дней).
    Если данных мало — честно «недостатньо даних», пост не хвастается.
    """
    lead = db.siren_lead_stats(days=7)
    lines: list[str] = ["🎯 <b>Точність прогнозів</b> — тиждень\n"]

    acc = public_accuracy(db, now=now)
    if acc.get("available") and acc.get("total"):
        pct = round(100 * int(acc["hits"]) / int(acc["total"]))
        lines.append(
            f"• Відбій у межах медіани: <b>{pct}%</b> "
            f"({acc['hits']}/{acc['total']} епізодів, "
            f"{acc.get('regions', 0)} регіонів)."
        )
    else:
        lines.append("• Прогнози відбоїв: недостатньо даних за тиждень.")

    if lead.get("episodes"):
        lead_min = lead["avg_lead_sec"] / 60
        line = (
            f"• Випередження офіційної тривоги: <b>{lead['before_count']} з "
            f"{lead['episodes']}</b> епізодів"
        )
        if lead_min > 0:
            line += f", у середньому на <b>{lead_min:.0f} хв</b>"
        lines.append(line + ".")
    else:
        lines.append("• Випередження сирени: офіційних епізодів за тиждень не було.")

    lines.append(
        "\nМетрики відкриті: рахуємо з історії БД, без підтасувань. "
        "Сирени — головне джерело, наші прогнози допомагають готуватись."
    )
    return "\n".join(lines)
