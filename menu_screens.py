"""menu_screens.py — екрани кнопкового меню бота: цілі, статистика, історія, наслідки, сторож, звук.

Усе, що користувач бачить у чаті з ботом, відкривається кнопкою; тут лише чисті функції
«дані → (текст HTML, кнопки)», щоб їх можна було тестувати без Telegram.
"""
from __future__ import annotations

import html
import time

from telethon import Button

from kyiv_time import fmt as kyiv_fmt
from regions import region_name

CB_MAIN = "main"
CB_TRACKS = "trk"
CB_STATS = "st2:"      # st2:today | yesterday | 7d | 30d
CB_HISTORY = "his"
CB_IMPACTS = "imp"
CB_GUARD = "grd"
CB_GUARD_ACT = "gra:"  # gra:on | off | 5 | 10 | 15 | 25 | loc
CB_SOUND = "snd"

WNAME = {"shahed": "Шахед", "uav": "БпЛА", "recon_drone": "Розвідувальний дрон", "cruise_missile": "Крилата ракета"}
PERIODS = (("today", "Сьогодні"), ("yesterday", "Вчора"), ("7d", "7 днів"), ("30d", "30 днів"))


def webapp_button(text: str, url: str):
    """Кнопка-WebApp, сумісна з telethon 1.36–1.44 та 1.45+."""
    try:
        from telethon.tl.types import KeyboardButtonWebView  # telethon < 1.45
        return KeyboardButtonWebView(text=text, url=url)
    except ImportError:
        from telethon.tl.types import InlineButtonTypeWebView, KeyboardInlineButton
        return KeyboardInlineButton(text, InlineButtonTypeWebView(url))


def site_base(map_url: str) -> str:
    return (map_url or "").rstrip("/")


def app_url(map_url: str, tab: str = "") -> str:
    base = site_base(map_url)
    return f"{base}/app.html" + (f"?tab={tab}" if tab else "") if base else ""


def _open_row(map_url: str, tab: str, label: str):
    """Ряд «відкрити в Mini App» (WebApp-кнопка), якщо адреса сайту задана."""
    url = app_url(map_url, tab)
    return [webapp_button(label, url)] if url else []


def _nav(map_url: str = "", tab: str = "", label: str = "📱 Відкрити в Mini App") -> list:
    rows = []
    r = _open_row(map_url, tab, label)
    if r:
        rows.append(r)
    rows.append([Button.inline("🏠 Меню", data=CB_MAIN)])
    return rows


def _ago(sec: float) -> str:
    return "щойно" if sec < 90 else f"{round(sec / 60)} хв тому" if sec < 3600 else f"{int(sec // 3600)} год {round(sec % 3600 / 60)} хв тому"


# ---------------- цілі зараз ----------------

def tracks_screen(db, map_url: str = "", now: int | None = None) -> tuple[str, list]:
    from health_server import radar_payload
    now = int(now if now is not None else time.time())
    data = radar_payload(db, 120, now)
    lines = []
    for t in data["tracks"]:
        if t["status"] == "stale" or not t["points"]:
            continue
        last = t["points"][-1]
        age = now - last["ts"]
        name = WNAME.get(t["weapon"], "БпЛА") + (" · реактивний" if t["profile"] == "reactive" else "") + (f" ×{t['count']}" if t["count"] > 1 else "")
        if t.get("dest"):
            where = f"→ {t['dest']['name']}"
        elif t["heading_text"]:
            where = f"{last['place'] or 'позиція невідома'}, курс: {t['heading_text']}"
        else:
            where = last["place"] or "позиція невідома"
        speed = f" · ~{round(t['speed_kmh'])} км/год" if t["speed_kmh"] else ""
        fc = ""
        if t["forecast"]:
            fc = "\n   ⏱ можливо: " + ", ".join(f"{f['name']} ~{f['eta_min']} хв" for f in t["forecast"][:3])
        lines.append((age, f"🛸 <b>{html.escape(name)}</b> {html.escape(where)}{speed} · {_ago(age)}{fc}"))
    ev = [e for e in data["events"] if now - e["ts"] <= 30 * 60][:8]
    names = {"kab": "💣 КАБ", "ballistic": "🚀 Балістика", "missile": "🚀 Ракета", "air_missile": "🚀 Ракета", "coastal_missile": "🚀 Ракета",
             "aviation": "✈️ Авіація", "tac_aviation": "✈️ Тактична авіація", "strat_aviation": "✈️ Стратегічна авіація", "fpv": "✦ FPV",
             "mlrs": "💥 РСЗО", "artillery": "💥 Артилерія", "explosion": "◉ Вибух", "air_defense": "◉ Робота ППО", "recon_drone": "👁 Розвідувальний дрон"}
    for e in ev:
        lines.append((now - e["ts"], f"{names.get(e['weapon'], e['weapon'])}{' ×' + str(e['count']) if e['count'] > 1 else ''} — "
                      f"{'у напрямку ' if e['toward'] else ''}{html.escape(e['place'] or 'область')} · {_ago(now - e['ts'])}"))
    lines.sort(key=lambda x: x[0])
    if not lines:
        text = "🛰 <b>Цілі на карті</b>\n\nЗараз активних цілей немає."
    else:
        text = f"🛰 <b>Цілі на карті</b> ({len(lines)})\n\n" + "\n\n".join(l for _, l in lines[:12])
        if len(lines) > 12:
            text += f"\n\n… і ще {len(lines) - 12} — на карті"
    text += "\n\n<i>Орієнтовно: позиції зі зведень каналів, не радар.</i>"
    rows = []
    if site_base(map_url):
        rows.append([webapp_button("🗺 Відкрити карту", site_base(map_url) + "/")])
    rows.append([Button.inline("🔄 Оновити", data=CB_TRACKS), Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


# ---------------- статистика ----------------

def _cat_line(c: dict, cats: dict) -> str:
    parts = [f"{cats[k]['icon']} {cats[k]['name']} <b>{c[k]}</b>" for k in cats if c.get(k)]
    return " · ".join(parts) or "нічого не зафіксовано"


def stats_screen(db, period: str = "today", map_url: str = "", now: int | None = None) -> tuple[str, list]:
    from stats_engine import stats_payload
    d = stats_payload(db, now)
    period = period if period in dict(PERIODS) else "today"
    p = d["periods"][period]
    top = d["regions"][:5]
    peak = max(range(24), key=lambda h: d["hours"][h]) if any(d["hours"]) else None
    text = (f"📊 <b>Статистика загроз — {dict(PERIODS)[period].lower()}</b>\n\n"
            f"Сигналів: <b>{p['total']}</b>\n{_cat_line(p, d['cats'])}\n")
    if top:
        text += "\n<b>Найбільше (7 днів):</b>\n" + "\n".join(f"• {html.escape(r['name'])} — {r['n']}" for r in top)
    if peak is not None:
        text += f"\n\n🕒 Пік активності за 7 днів: <b>{peak:02d}:00–{(peak + 1) % 24:02d}:00</b> (київський час)"
    text += f"\n🔴 Активні зараз: {len(d['active_regions'])} обл.\n\n<i>{html.escape(d['note'])}</i>"
    seg = [Button.inline(("• " if k == period else "") + n, data=CB_STATS + k) for k, n in PERIODS]
    return text, [seg[:2], seg[2:]] + _nav(map_url, "stats")


# ---------------- історія ----------------

def history_screen(db, map_url: str = "", now: int | None = None) -> tuple[str, list]:
    from stats_engine import stats_payload
    d = stats_payload(db, now)
    lines = ["💥 <b>Історія атак</b> (14 днів)\n"]
    for x in d["days"]:
        dd = ".".join(x["date"].split("-")[::-1][:2])
        if not x["total"]:
            lines.append(f"<b>{dd}</b> — без сигналів")
            continue
        lines.append(f"<b>{dd}</b> — {x['total']} сигн. · {x['first']}–{x['last']}\n   {_cat_line(x['cats'], d['cats'])}\n   {html.escape(', '.join(x['regions']))}")
    lines.append(f"\n<i>{html.escape(d['note'])}</i>")
    return "\n".join(lines)[:4000], _nav(map_url, "hist")


# ---------------- наслідки ----------------

def impacts_screen(db, map_url: str = "", delay_s: int | None = None, now: int | None = None) -> tuple[str, list]:
    import impact_log
    d = impact_log.impacts_payload(db, now, delay_s if delay_s is not None else impact_log.DEFAULT_DELAY_S)
    head = f"🔥 <b>Наслідки</b> (14 днів, із затримкою {d['delay_min']} хв)\n"
    if not d["items"]:
        body = ("\nПідтверджених повідомлень про наслідки поки немає.\n\n"
                "Тут з'являються лише повідомлення офіційних джерел — із затримкою і без адрес та назв об'єктів: "
                "точки влучань у реальному часі допомагають ворогу коригувати вогонь.")
    else:
        out = [f"\nЗагиблих: <b>{d['totals']['killed']}</b> · поранених: <b>{d['totals']['injured']}</b>\n"]
        for i in d["items"][:10]:
            where = html.escape(i["place"] or i["oblast"]) + (f" · {html.escape(i['oblast'])}" if i["place"] and i["oblast"] else "")
            bits = []
            if i["killed"]:
                bits.append(f"загинули: {i['killed']}")
            if i["injured"]:
                bits.append(f"поранені: {i['injured']}")
            if i["damage"]:
                bits.append(", ".join(i["damage"]))
            out.append(f"📍 <b>{where}</b> — {i['when']}\n   {html.escape('; '.join(bits)) or 'без уточнень'}")
        body = "\n".join(out)
    return (head + body + f"\n\n<i>{html.escape(d['note'])}</i>")[:4000], _nav(map_url, "imp")


# ---------------- сторож ----------------

RADII = (5, 10, 15, 25)


def guard_screen(db, uid: int, map_url: str = "") -> tuple[str, list]:
    g = db.get_guard(uid)
    on = bool(g and g["enabled"])
    if not on:
        status = "🔕 Сторож вимкнено."
    else:
        where = "очікую геопозицію" if g["lat"] is None else ("live-геопозиція" if g["live"] else "фіксована точка")
        status = f"🛡 Сторож увімкнено · радіус {g['radius_km']:g} км · {where}"
    text = ("🛡 <b>Гео-сторож</b>\n\n" + status +
            "\n\nБот стежить за цілями відносно твоєї точки й пише одразу, коли ціль наближається "
            "(за замовчуванням ≤10 км і курс на тебе). Координати зберігаються лише поки сторож увімкнено.\n\n"
            "1. Увімкни сторож.\n2. Натисни «📍 Надіслати геопозицію» (краще live: 📎 → Локація → «Транслювати»).")
    rad = [Button.inline(("✅ " if on and abs(g["radius_km"] - r) < 0.1 else "") + f"{r} км", data=f"{CB_GUARD_ACT}{r}") for r in RADII]
    rows = [[Button.inline("🔕 Вимкнути" if on else "🛡 Увімкнути", data=CB_GUARD_ACT + ("off" if on else "on"))]]
    if on:
        rows.append(rad)
        rows.append([Button.inline("📍 Надіслати геопозицію", data=CB_GUARD_ACT + "loc")])
    rows.append([Button.inline("🔔 Звук тривоги", data=CB_SOUND), Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


def guard_apply(db, uid: int, action: str) -> str:
    """Виконати дію кнопки сторожа; повертає підказку для toast."""
    if action == "on":
        db.set_guard(uid, True)
        return "🛡 Сторож увімкнено"
    if action == "off":
        db.set_guard(uid, False)
        return "🔕 Сторож вимкнено, координати стерто"
    if action.isdigit() and int(action) in RADII:
        db.set_guard(uid, True, float(action))
        return f"🛡 Радіус {action} км"
    return ""


def sound_screen(map_url: str = "") -> tuple[str, list]:
    from guard_handlers import SOUND_HELP
    return SOUND_HELP, [[Button.inline("🛡 Гео-сторож", data=CB_GUARD), Button.inline("🏠 Меню", data=CB_MAIN)]]
