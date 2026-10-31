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
CB_PLACES = "plc"
CB_APP = "app"
CB_FAMILY = "fam"          # fam · fam:inv · fam:del:<id> · fam:safe
CB_PLACE_ACT = "pla:"   # pla:add:home|work|family|other · pla:del:<id> · pla:rad:<км>
CB_SHELTER_FIND = "shl"  # shl · shl:p<id> · shl:g · shl:loc · shl:city

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
    text += f"\n🔴 Активні зараз: {len(d['active_regions'])} обл."
    src = d.get("sources") or {}
    if src.get("total"):
        text += f"\n📡 Джерела: активні {src['active']} із {src['total']} каналів"
    acc = d.get("accuracy") or {}
    text += (f"\n🎯 Точність прогнозів радара (7 днів): <b>{acc['pct']}%</b> ({acc['hits']} з {acc['resolved']})"
             if acc.get("pct") is not None else "\n🎯 Точність прогнозів: накопичуємо статистику")
    text += f"\n\n<i>{html.escape(d['note'])}</i>"
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
    hits = [i for i in d["items"] if i["kind"] == "hit"]
    blasts = [i for i in d["items"] if i["kind"] != "hit"]
    head = "🔥 <b>Наслідки</b> (14 днів)\n"
    if not d["items"]:
        body = "\nПовідомлень про влучання та наслідки поки немає. Вони з'являються тут одразу, як тільки канали про них напишуть."
    else:
        t = d["totals"]
        out = [f"\nВлучань: <b>{t['hits']}</b> · загиблих: <b>{t['killed']}</b> · поранених: <b>{t['injured']}</b>"
               + (f" · дітей: <b>{t['children']}</b>" if t.get("children") else "")
               + (f"\n<i>У {t['unclear']} повідомленнях кількість уточнюється — у підсумок не входить.</i>" if t.get("unclear") else "") + "\n"]
        for i in hits[:10]:
            where = html.escape(i["place"] or i["oblast"]) + (f" · {html.escape(i['oblast'])}" if i["place"] and i["oblast"] else "")
            bits = impact_log.casualty_line(i["killed"], i["injured"], i.get("children"), i.get("flags", ""))
            if i["damage"]:
                bits.append("🏚 " + ", ".join(i["damage"]))
            out.append(f"💥 <b>{where}</b> — {i['when']}\n" + ("\n".join("   " + html.escape(b) for b in bits) or "   влучання, подробиць немає"))
        if blasts:
            out.append("\n◉ <b>Вибухи (чутно):</b> " + ", ".join(dict.fromkeys(html.escape(b["place"] or b["oblast"]) for b in blasts[:8])))
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
            "Найпростіше: «🏙 Задати місто» — напиши назву, і сторож стежитиме за ним (геопозиція не потрібна). "
            "Або «📍 Надіслати геопозицію» (краще live: 📎 → Локація → «Транслювати»). "
            "«🔔 Тест сповіщення» покаже, як виглядає попередження на твоєму телефоні.")
    rad = [Button.inline(("✅ " if on and abs(g["radius_km"] - r) < 0.1 else "") + f"{r} км", data=f"{CB_GUARD_ACT}{r}") for r in RADII]
    rows = [[Button.inline("🔕 Вимкнути" if on else "🛡 Увімкнути", data=CB_GUARD_ACT + ("off" if on else "on"))],
            [Button.inline("🏙 Задати місто", data=CB_GUARD_ACT + "city"), Button.inline("🔔 Тест сповіщення", data=CB_GUARD_ACT + "test")]]
    if on:
        rows.append(rad)
        rows.append([Button.inline("📍 Надіслати геопозицію", data=CB_GUARD_ACT + "loc")])
    import guard_places
    rep_on = uid in guard_places.persistent_users(db)
    rows.append([Button.inline(("✅ " if rep_on else "") + "🔁 Повтор попередження", data=CB_GUARD_ACT + "rep")])
    rows.append([Button.inline("📍 Мої місця", data=CB_PLACES), Button.inline("🏚 Найближче укриття", data=CB_SHELTER_FIND)])
    rows.append([Button.inline("👪 Родина", data=CB_FAMILY), Button.inline("✅ Я в укритті", data=f"{CB_FAMILY}:safe")])
    rows.append([Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


def guard_apply(db, uid: int, action: str) -> str:
    """Виконати дію кнопки сторожа; повертає підказку для toast."""
    if action == "rep":
        import guard_places
        return "🔁 Повтор попередження увімкнено" if guard_places.toggle_persistent(db, uid) else "🔁 Повтор попередження вимкнено"
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


def test_alert_text(map_url: str = "") -> str:
    """Зразок справжнього сповіщення сторожа (той самий шаблон) — перевірити звук і режим «Не турбувати»."""
    from proximity import guard_message
    from track_engine import Risk
    risk = Risk(track_id="t", weapon="shahed", level="imminent", distance_km=8.0, bearing_from_point=180.0, approaching=True,
                cpa_km=0.5, eta_min=3, speed_kmh=180.0, speed_source="measured", count=1)
    return "🧪 ТЕСТ СПОВІЩЕННЯ — це зразок, реальної загрози немає\n\n" + guard_message(risk, map_url)


def set_city_guard(db, uid: int, name: str) -> str:
    """Увімкнути сторож на фіксованій точці — місті за назвою. Повертає відповідь користувачу."""
    from regions import region_name
    from threat_parser import locate
    geo = locate(name.strip(), "")
    if geo is None or geo.precision not in ("place", "approx"):
        return "Не знайшов такого населеного пункту. Напиши назву інакше (наприклад «Харків» або «Біла Церква»)."
    db.set_guard(uid, True)
    db.update_guard_location(uid, geo.lat, geo.lon, False)
    g = db.get_guard(uid) or {}
    extra = " (є кілька однойменних — взято найбільший)" if geo.precision == "approx" else ""
    return (f"🛡 Сторож стежить за: <b>{geo.name}</b> ({region_name(geo.oblast)}){extra}\n"
            f"Радіус {g.get('radius_km', 10):g} км. Змінити радіус або місто — в меню «🛡 Гео-сторож».")


# ---------------- мої місця ----------------

def places_screen(db, uid: int, notice: str = "") -> tuple[str, list]:
    import guard_places
    places = guard_places.list_places(db, uid)
    text = "📍 <b>Мої місця</b>\n\n"
    if notice:
        text += notice + "\n\n"
    if places:
        text += "Сторож перевіряє всі ці точки й пише з назвою місця:\n" + "\n".join(
            f"• {html.escape(p['name'])} — радіус {p['radius_km']:g} км" for p in places)
    else:
        text += "Поки порожньо. Додай дім, роботу чи рідних — сторож попередить саме про те місце, якому щось летить."
    text += (f"\n\nДо {guard_places.MAX_PLACES} місць. Додати: обери назву й надішли місто текстом або геопозицію "
             "(координати зберігаються лише для твоїх місць і стираються видаленням).")
    rows = [[Button.inline("❌ " + p["name"], data=f"{CB_PLACE_ACT}del:{p['id']}")] for p in places]
    rows.append([Button.inline(v, data=f"{CB_PLACE_ACT}add:{k}") for k, v in (("home", "➕ 🏠 Дім"), ("work", "➕ 💼 Робота"))])
    rows.append([Button.inline("➕ 👪 Рідні", data=f"{CB_PLACE_ACT}add:family"), Button.inline("➕ Інше", data=f"{CB_PLACE_ACT}add:other")])
    if places:
        rows.append([Button.inline(f"{r} км", data=f"{CB_PLACE_ACT}rad:{r}") for r in RADII])
    rows.append([Button.inline("🛡 Гео-сторож", data=CB_GUARD), Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


def place_label(kind: str) -> str:
    import guard_places
    return {"home": guard_places.LABELS["home"], "work": guard_places.LABELS["work"], "family": guard_places.LABELS["family"]}.get(kind, "")


def place_add_from_text(db, uid: int, label: str, text: str) -> tuple[bool, str]:
    """Текст «місто» або «Назва: місто». Повертає (успіх, відповідь)."""
    import guard_places
    from regions import region_name
    from threat_parser import locate
    name, city = label, text
    if ":" in text:
        left, _, right = text.partition(":")
        name, city = (left.strip() or label), right.strip()
    geo = locate(city.strip(), "")
    if geo is None or geo.precision not in ("place", "approx"):
        return False, "Не знайшов такого населеного пункту. Напиши назву інакше (наприклад «Харків») або надішли геопозицію."
    ok, msg = guard_places.add_place(db, uid, name or geo.name, geo.lat, geo.lon)
    return ok, (f"✅ Збережено: <b>{html.escape(msg)}</b> — {geo.name} ({region_name(geo.oblast)})" if ok else msg)


# ---------------- найближче укриття ----------------

def shelter_pick_screen(db, uid: int) -> tuple[str, list]:
    import guard_places
    places = guard_places.list_places(db, uid)
    g = db.get_guard(uid)
    text = ("🏚 <b>Найближче укриття</b>\n\nВід якої точки шукати? Обери збережене місце, надішли геопозицію або назви місто.")
    rows = [[Button.inline(p["name"], data=f"{CB_SHELTER_FIND}:p{p['id']}")] for p in places]
    if g and g["enabled"] and g["lat"] is not None:
        rows.append([Button.inline("🛡 Точка сторожа", data=f"{CB_SHELTER_FIND}:g")])
    rows.append([Button.inline("📍 Надіслати геопозицію", data=f"{CB_SHELTER_FIND}:loc"), Button.inline("🏙 Назвати місто", data=f"{CB_SHELTER_FIND}:city")])
    rows.append([Button.inline("🛡 Гео-сторож", data=CB_GUARD), Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


def shelter_point(db, uid: int, token: str):
    """'p<id>' | 'g' → (lat, lon, label) або None."""
    import guard_places
    if token.startswith("p") and token[1:].isdigit():
        for p in guard_places.list_places(db, uid):
            if p["id"] == int(token[1:]):
                return p["lat"], p["lon"], p["name"]
    if token == "g":
        g = db.get_guard(uid)
        if g and g["lat"] is not None:
            return g["lat"], g["lon"], "точка сторожа"
    return None


async def shelter_reply(lat: float, lon: float, label: str = "", finder=None) -> tuple[str, list]:
    """Текст і кнопки з найближчими укриттями; finder (для тестів) замінює запит до карт."""
    import shelters
    results = await (finder or shelters.nearest)(lat, lon)
    rows = []
    for r in (results or [])[:3]:
        rows.append([Button.url(f"🚶 {r['name'] or 'Укриття'} · {shelters.fmt_distance(r['m'])}", shelters.route_link(r["lat"], r["lon"]))])
    links = shelters.search_links(lat, lon)
    rows.append([Button.url("🔎 Шукати на карті (Google)", links["google"]), Button.url("🗺 OSM", links["osm"])])
    rows.append([Button.inline("🏚 Інша точка", data=CB_SHELTER_FIND), Button.inline("🏠 Меню", data=CB_MAIN)])
    return shelters.format_text(results, label), rows


# ---------------- родина ----------------

def family_screen(db, uid: int, notice: str = "") -> tuple[str, list]:
    import family
    ms_ = family.members(db, uid)
    text = "👪 <b>Родина</b>\n\n"
    if notice:
        text += notice + "\n\n"
    if ms_:
        text += "Вам обом надходять короткі повідомлення одне про одного:\n" + "\n".join(f"• {html.escape(family.name_of(db, m))}" for m in ms_)
    else:
        text += "Поки нікого. Створи запрошення й надішли його близькій людині — вона відкриє посилання в боті й ви стаєте родиною."
    text += ("\n\nЩо надходить: «⚠️ небезпечно біля 🏠 Дім» (без координат — лише назва місця, відстань і час) та «✅ в укритті», "
             f"коли хтось натискає «Я в укритті». До {family.MAX_MEMBERS} людей. Вийти можна будь-коли — ❌.")
    rows = [[Button.inline("❌ " + family.name_of(db, m), data=f"{CB_FAMILY}:del:{m}")] for m in ms_]
    rows.append([Button.inline("➕ Створити запрошення", data=f"{CB_FAMILY}:inv")])
    if ms_:
        import family_status
        text += "\n\n<b>Статуси</b>\n" + family_status.board_text(db, uid)
    rows.append([Button.inline("🏚 В укритті", data=f"{CB_FAMILY}:st:shelter"), Button.inline("✅ В безпеці", data=f"{CB_FAMILY}:st:safe")])
    rows.append([Button.inline("🟢 На зв'язку", data=f"{CB_FAMILY}:st:ok"), Button.inline("🆘 Потрібна допомога", data=f"{CB_FAMILY}:st:help")])
    rows.append([Button.inline("✅ Я в укритті", data=f"{CB_FAMILY}:safe")])
    rows.append([Button.inline("🛡 Гео-сторож", data=CB_GUARD), Button.inline("🏠 Меню", data=CB_MAIN)])
    return text, rows


def safe_button() -> list:
    return [Button.inline("✅ Я в укритті", data=f"{CB_FAMILY}:safe")]


SITE_URL = "https://wizard732.github.io/AirRadar-AI"
APK_URL = SITE_URL + "/AirRadar.apk"

APP_HELP = (
    "📱 <b>Додаток AirRadar для Android</b>\n\n"
    "Свій додаток із гучним сигналом: працює у фоні, показує карту й попередження про цілі, що летять до вашої точки. "
    "Безкоштовно, без реєстрації.\n\n"
    "<b>Швидкий старт</b>\n"
    "1. Натисни «⬇️ Завантажити AirRadar (APK)» і встанови файл (дозволь «Невідомі джерела» для браузера).\n"
    "2. Відкрий додаток → «Ще» → скопіюй команду <code>/pair КОД</code> і надішли її сюди, боту.\n"
    "3. Увімкни 🛡 Гео-сторож у меню й задай свою точку.\n\n"
    "📖 Докладна інструкція — кнопка «Повна інструкція». "
    "iPhone: звичайного додатка без платного акаунта Apple немає — використай ntfy і карту «На екран Додому» (див. інструкцію).\n\n"
    "Твоя тема ntfy (секретна, нікому не передавай): <code>{topic}</code>. Відключити пуші: /unpair."
)

GUIDE = [
    ("Що це і як працює",
     "🧭 <b>Як працює AirRadar</b>\n\n"
     "• Бот читає відкриті канали зі зведеннями про повітряні загрози, розбирає повідомлення, будує <b>треки цілей</b> "
     "(курс, швидкість, час підльоту) і показує їх на карті.\n"
     "• Це <b>орієнтовні</b> дані зі зведень, а не радар: місце часто назване з точністю до міста або області.\n"
     "• <b>Гео-сторож</b> порівнює цілі з твоєю точкою й попереджає: ціль летить до тебе, скільки хвилин приблизно, з якого боку.\n"
     "• Є три рівні: 🟡 <b>спостереження</b> (може пройти поруч), 🟠 <b>небезпека</b> (курс на тебе, є час у хвилинах), "
     "🔴 <b>над вами</b>.\n\n"
     "⚠️ Бот <b>не замінює</b> офіційну сирену й застосунки «Повітряна тривога». Якщо лунає тривога — одразу в укриття."),
    ("Android: встановлення",
     "🤖 <b>Android: встановлення за 6 кроків</b>\n\n"
     "1. Завантаж APK кнопкою нижче (файл ≈ 5 МБ).\n"
     "2. Відкрий завантажений файл. Якщо Android питає — дозволь «Встановлювати невідомі програми» для браузера чи «Файлів».\n"
     "3. Запусти AirRadar → дозволь <b>Сповіщення</b>.\n"
     "4. Відкрий вкладку <b>«Ще»</b>: там блок «Пуш-сповіщення» і команда <code>/pair КОД</code>. "
     "Натисни «Скопіювати команду» й надішли її цьому боту. Повідомлення з кодом бот видалить.\n"
     "5. Натисни «Не вимикати у фоні» й дозволь роботу без обмежень батареї — інакше телефон може приспати додаток і сигнал запізниться.\n"
     "6. Натисни «Перевірити гучний сигнал»: має прозвучати сигнал і прийти сповіщення «🧪 ТЕСТ».\n\n"
     "Готово: тривоги й попередження сторожа приходитимуть у додаток навіть із вимкненим екраном."),
    ("Звуки та «Не турбувати»",
     "🔊 <b>Звуки та режим «Не турбувати»</b>\n\n"
     "У додатку два окремі гучні канали:\n"
     "• <b>«Небезпека поруч»</b> — фірмовий сигнал «бі-бі-бі». Лунає лише тоді, коли ціль летить до <b>твоєї</b> точки "
     "(рівні 🟠 і 🔴).\n"
     "• <b>«Тривога»</b> — сирена загальної повітряної тривоги у твоїй області.\n"
     "• <b>«Попередження»</b> — 🟡 м'який звук (може пройти поруч, «можливо до твого міста»).\n\n"
     "Щоб сигнал лунав навіть у режимі «Не турбувати»: Налаштування Android → Додатки → AirRadar → Сповіщення → "
     "канал «Небезпека поруч» → увімкни «Ігнорувати Не турбувати» (на деяких телефонах: Налаштування → Не турбувати → Винятки → Додатки).\n"
     "Гучність цих сигналів іде по каналу <b>будильника</b>, тож перевір, що гучність будильника не на нулі.\n\n"
     "Із Telegram гучний звук неможливий — для гучного сигналу потрібен додаток або ntfy."),
    ("Гео-сторож і місця",
     "🛡 <b>Гео-сторож</b>\n\n"
     "Меню → «🛡 Гео-сторож». Дві частини:\n"
     "1. <b>Моя точка</b> — надішли геопозицію (скріпка → «Геопозиція») або назви місто. Для живого стеження вмикай трансляцію геопозиції "
     "(вона діє обмежений час, а точка-місто — постійно).\n"
     "2. <b>«📍 Мої місця»</b> — до 5 місць: дім, робота, рідні. Попередження приходять по кожному окремо з назвою.\n\n"
     "<b>Радіус</b> (5–25 км) — відстань, де ціль уже вважається небезпечною. Для швидких цілей (ракети, реактивні дрони) "
     "бот попереджає за часом підльоту, а не лише за кілометрами.\n"
     "<b>🔁 Повтор попередження</b> — до двох нагадувань, поки небезпека триває, доки не натиснеш «✅ Я в укритті».\n"
     "Також сторож повідомляє про КАБ, ракети, авіацію й РСЗВ поруч — без курсу, але з відстанню й часом.\n"
     "Геопозицію зберігаємо лише для сторожа, вимкнеш його — координати стираються."),
    ("Як читати попередження",
     "🧮 <b>Як читати попередження</b>\n\n"
     "<i>«🟠 Шахед: 8 км, північ, курс на вас, орієнтовно 3–6 хв (~180 км/год, типова)»</i>\n\n"
     "• <b>8 км, північ</b> — відстань і сторона, звідки летить ціль.\n"
     "• <b>курс на вас</b> — напрямок руху веде до твоєї точки. «Курс невідомий» — напрямок не встановлено, вважай, що на тебе.\n"
     "• <b>3–6 хв</b> — діапазон часу підльоту: «найраніше — найпізніше». Орієнтуйся на менше число.\n"
     "• <b>типова</b> швидкість — виміру немає, взято типову для цього типу зброї (шахед ≈180, реактивний ≈550, "
     "крилата ракета ≈800, балістика ≈2000 км/год).\n"
     "• <b>«може пройти за 6 км від вас»</b> — 🟡 проліт поруч, не пряме влучання.\n\n"
     "Точність обмежена: джерела дають снапшоти, а не координати борта. Тому завжди закладай запас і не чекай «ідеального» повідомлення."),
    ("Родина та укриття",
     "👪 <b>Родина та укриття</b>\n\n"
     "• <b>«👪 Родина»</b> — створи запрошення й перешли близьким. Коли для тебе 🟠/🔴, їм прийде коротке «небезпечно біля …» без твоїх координат.\n"
     "• <b>«✅ Я в укритті»</b> — кнопка під небезпечним попередженням: вимикає повтори на 3 години й сповіщає родину, що ти в безпеці.\n"
     "• <b>«🏚 Найближче укриття»</b> — шукає укриття з бази OpenStreetMap (≈7 тис. точок). Дані неповні — "
     "завжди звіряйся з тим, що бачиш на місці.\n"
     "• Коли в твоїй області оголошено тривогу, приходить «ПОВІТРЯНА ТРИВОГА», а після неї — «ВІДБІЙ».\n\n"
     "Тема ntfy секретна: той, хто її знає, бачить твої сповіщення."),
    ("iPhone",
     "🍎 <b>iPhone</b>\n\n"
     "Власний додаток для iPhone без платного акаунта Apple неможливий, але пуші й карта працюють так:\n"
     "1. Встанови безкоштовний <b>ntfy</b> з App Store.\n"
     "2. У цьому розділі «📱 Додаток» натисни «🔔 Підписатись (ntfy)» → «Open in app». Або додай тему вручну.\n"
     "3. У ntfy дозволь сповіщення зі звуком, а режим «Фокус» налаштуй так, щоб ntfy був у винятках.\n"
     "4. Карта як додаток: Safari → відкрий карту → «Поділитись» → «На екран Додому».\n\n"
     "Обмеження: iOS може затримувати фонові пуші, режим «Фокус» глушить сигнал — додай ntfy у винятки."),
    ("Проблеми й відповіді",
     "🛠 <b>Щось не працює?</b>\n\n"
     "• <b>Немає звуку</b>: перевір гучність будильника, канал «Небезпека поруч» (не вимкнений), режим «Не турбувати».\n"
     "• <b>Сигнал запізнюється або не приходить</b>: «Не вимикати у фоні» в додатку, а на Xiaomi/Huawei/Samsung ще додай AirRadar в «Автозапуск» "
     "і прибери з «сплячих» додатків.\n"
     "• <b>Додаток не бачить пушів</b>: надішли боту <code>/pair КОД</code> ще раз (або «Новий код» у додатку).\n"
     "• <b>Попереджень немає, а тривога є</b>: у зведеннях може не бути інформації про ціль — це нормально, діє офіційна сирена.\n"
     "• <b>Не туди показує точку</b>: перевір «Мої місця» й радіус.\n"
     "• Вимкнути пуші: /unpair. Стерти геопозицію: вимкни 🛡 Гео-сторож.\n\n"
     "Питання й помилки — у каналі @AirRadarAI."),
]
NTFY_ANDROID = "https://play.google.com/store/apps/details?id=io.heckel.ntfy"
NTFY_IOS = "https://apps.apple.com/app/ntfy/id1625396347"


def app_screen(db, uid: int, map_url: str = "") -> tuple[str, list]:
    import push_fcm
    topic = push_fcm.ntfy_topic_for(db, uid)
    text = APP_HELP.replace("{topic}", topic)
    base = site_base(map_url) or SITE_URL
    rows = [[Button.url("⬇️ Завантажити AirRadar (APK)", base + "/AirRadar.apk")],
            [Button.inline("📖 Повна інструкція", data=f"{CB_APP}:g:0")],
            [Button.url("🔔 Підписатись (ntfy)", f"{push_fcm.NTFY_URL}/{topic}")],
            [Button.url("🤖 ntfy для Android", NTFY_ANDROID), Button.url("🍎 ntfy для iPhone", NTFY_IOS)],
            [Button.url("🗺 Карта як додаток", base + "/")],
            [Button.inline("🏠 Меню", data=CB_MAIN)]]
    return text, rows


def guide_screen(page: int, map_url: str = "") -> tuple[str, list]:
    """Повна інструкція: сторінки GUIDE з навігацією ◀️ ▶️ та кнопкою завантаження."""
    page = max(0, min(len(GUIDE) - 1, page))
    title, body = GUIDE[page]
    text = body + f"\n\n<i>Сторінка {page + 1} з {len(GUIDE)}</i>"
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ Назад", data=f"{CB_APP}:g:{page - 1}"))
    if page < len(GUIDE) - 1:
        nav.append(Button.inline("Далі ▶️", data=f"{CB_APP}:g:{page + 1}"))
    base = site_base(map_url) or SITE_URL
    rows = [nav] if nav else []
    rows += [[Button.url("⬇️ Завантажити AirRadar (APK)", base + "/AirRadar.apk")],
             [Button.inline("📱 Додаток", data=CB_APP), Button.inline("🏠 Меню", data=CB_MAIN)]]
    return text, rows
