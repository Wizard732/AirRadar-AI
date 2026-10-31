"""Evidence-first public alerts: direct source facts, never forecasts.

Компактный формат публичного поста:
    🔴 Київ та область | БПЛА
    🕒 09:21
    📍 Рух: цілі прямують з Сумська обл. на Київ та область   (если известен маршрут)
    ⚠️ Статус: підтверджено 2 незалежними джерелами
    <текст источника, ≤300 знаков>
    📡 Джерело: AirRadar AI   (Publisher делает эту строку кликабельной ссылкой)

Без ETA-прогнозов и вероятностей. Только прямые факты источника. Имена
исходных каналов в пост НЕ попадают: строка «Джерело» — это наш канал
(пассивный брендинг), а кнопка подписки прикрепляется Publisher'ом.
"""

from __future__ import annotations

import time

from incident_fusion import IncidentFact
from kyiv_time import fmt as kyiv_fmt
from regions import region_name
from weapon_classes import weapon_eta, weapon_severity

# Короткие UA-названия классов оружия для публичных постов (без эмодзи-
# заголовков weapon_label — эмодзи задаётся отдельно по критичности).
WEAPON_SHORT: dict[str, str] = {
    "fpv": "БПЛА (FPV)",
    "shahed": "БПЛА (Shahed)",
    "recon_drone": "БПЛА (розвідка)",
    "uav": "БПЛА",
    "ballistic": "Балістика",
    "cruise_missile": "Крилаті ракети",
    "air_missile": "Авіаційні ракети",
    "coastal_missile": "Берегові ракети",
    "kab": "КАБ",
    "tac_aviation": "Тактична авіація",
    "strat_aviation": "Стратегічна авіація",
    "aviation": "Авіація",
    "mlrs": "РСЗО",
    "artillery": "Артилерія",
    "explosion": "Прильот/вибух",
    "air_defense": "Робота ППО",
    "decoy": "Хибна ціль",
    "alert": "Тривога",
    "stand_down": "Відбій",
}

# Эмодзи по базовой критичности класса оружия.
_SEVERITY_EMOJI: dict[str, str] = {
    "CRITICAL": "🔴",
    "HIGH": "🔴",
    "MODERATE": "🟡",
    "LOW": "⚪",
    "ALL_CLEAR": "🟢",
}

# Строка источника в посте: всегда наш канал (пассивный брендинг вместо
# упоминания исходного канала мониторинга). Кнопка «Підписатися» вешается
# Publisher'ом (см. publisher.py, promo_url).
SOURCE_BRAND_LINE = "📡 Джерело: AirRadar AI"


def _status(confirmation: dict) -> str:
    """Строка статуса; '' = строку не рисуем вовсе.

    Одиночное сообщение одного источника НЕ помечается никак: строка
    «⚠️ Статус: повідомлення одного джерела» в каждом посте — шум без
    пользы для читателя. Подтверждение (2+ источника / официальное)
    по-прежнему показываем — это ценная информация.
    """
    status = confirmation.get("status", "reported")
    if status == "officially_confirmed":
        return "підтверджено офіційним джерелом"
    if status == "corroborated":
        return f"підтверджено {confirmation.get('sources', 2)} незалежними джерелами"
    return ""


def _region_title(fact: IncidentFact) -> str:
    """Регион для заголовка; 'unknown'/пустой не показываем."""
    region = fact.destination_region
    if region == "multi":
        # Сборная сводка по нескольким областям — без ложной конкретики.
        return "Кілька областей"
    if region and region != "unknown":
        return region_name(region)
    return ""


_OBLAST_SHORT = {
    "kyivska": "Київщина", "sumska": "Сумщина", "chernigivska": "Чернігівщина", "poltavska": "Полтавщина",
    "kharkivska": "Харківщина", "dnipropetrovska": "Дніпропетровщина", "zaporizka": "Запоріжжя",
    "khersonska": "Херсонщина", "mykolaivska": "Миколаївщина", "odeska": "Одещина",
    "kirovohradska": "Кіровоградщина", "cherkaska": "Черкащина", "vinnytska": "Вінниччина",
    "zhytomyrska": "Житомирщина", "rivnenska": "Рівненщина", "volynska": "Волинь",
    "khmelnytska": "Хмельниччина", "ternopilska": "Тернопільщина", "lvivska": "Львівщина",
    "zakarpatska": "Закарпаття", "ivano_frankivska": "Прикарпаття", "chernivetska": "Буковина",
    "donetska": "Донеччина", "luhanska": "Луганщина", "crimea": "Крим",
}
_MAX_PLACES_PER_OBLAST = 6


def compact_roundup(text: str) -> str:
    """Сводка по областям → компактный блок без воды; '' если пост не сводка.

    Было: полный текст канала (иногда 1200 знаков с эмодзи-шапками и подписями).
    Стало: по строке на область — «Сумщина · 2: Боромля, Лебедин». Названия и числа
    только из распознанных наблюдений, ничего не додумываем.
    """
    from threat_parser import parse_post
    parsed = parse_post(text)
    obs = [o for o in parsed.observations if o.oblast and o.relation != "from"]
    if parsed.kind != "track" or len({o.oblast for o in obs}) < 2 and len(obs) < 4:
        return ""
    groups: dict[str, list] = {}
    for o in obs:
        groups.setdefault(o.oblast, []).append(o)
    lines = []
    for slug, items in groups.items():
        total = next((o.group_count for o in items if o.group_count), None)
        if total is None:
            counted = [o.count for o in items if o.count]
            total = sum(counted) if counted and len(counted) == len(items) else None
        places = []
        for o in items:
            if not o.place:
                continue
            name = _disp(o)
            label = f"{name} ({o.count})" if o.count and o.count > 1 and o.relation != "toward" else name
            if o.relation == "toward":
                label = (f"{o.count}× " if o.count and o.count > 1 else "") + f"→ {name}"
            elif o.relation == "via":
                label = f"через {name}"
            if label not in places:
                places.append(label)
        shown = places[:_MAX_PLACES_PER_OBLAST]
        more = len(places) - len(shown)
        tail = ", ".join(shown) + (f" +{more}" if more > 0 else "")
        head = _OBLAST_SHORT.get(slug, region_name(slug)) + (f" · {total}" if total else "")
        lines.append(f"{head}: {tail}" if tail else head)
    return "\n".join(lines)


def _disp(o) -> str:
    """Название пункта в именительном падеже («Одесу» → «Одеса»), если он есть в справочнике."""
    from threat_parser import locate
    if not o.place:
        return ""
    geo = locate(o.place, o.oblast) if o.precision in ("place", "approx") else None
    return geo.name if geo and geo.precision == "place" else o.place


def _short(slug: str) -> str:
    return _OBLAST_SHORT.get(slug, region_name(slug))


def _structured_body(parsed) -> tuple[str, list[str]]:
    """Разбор поста → (регионы для шапки, строки «Зараз / Курс / Звідки») для простого поста.

    Для сводок по нескольким областям строк нет — их рисует compact_roundup."""
    obs = [o for o in parsed.observations if o.oblast or o.place]
    regions = " · ".join(_short(x) for x in parsed.oblasts[:2]) + (f" +{len(parsed.oblasts) - 2}" if len(parsed.oblasts) > 2 else "")
    if len(parsed.oblasts) >= 2 or len(obs) >= 4:
        return regions, []
    now = [_disp(o) for o in obs if o.relation in ("at", "near") and o.place]
    aim = [_disp(o) for o in obs if o.relation == "toward" and o.place]
    via = [_disp(o) for o in obs if o.relation == "via" and o.place]
    lines = []
    if now:
        lines.append("📍 Зараз: " + ", ".join(dict.fromkeys(now)))
    if via:
        lines.append("↪️ Через: " + ", ".join(dict.fromkeys(via)))
    if aim:
        lines.append("➡️ Курс на: " + ", ".join(dict.fromkeys(aim)))
    elif not now and parsed.oblasts:
        lines.append("📍 Зараз: " + ", ".join(_short(x) for x in parsed.oblasts))
    origin = _origin_text(parsed)
    if origin:
        lines.append("↩️ Звідки: " + origin)
    return regions, lines


def _origin_text(parsed) -> str:
    places = [_disp(o) for o in parsed.observations if o.relation == "from" and o.place and not o.place.endswith("ька")]
    names = places or [_short(x) for x in parsed.origins]
    return ", ".join(dict.fromkeys(names))


def render_evidence_alert(
    *, text: str, source: str, event_ts: int, fact: IncidentFact, confirmation: dict,
    sources: list[str] | None = None, track: dict | None = None, lag_s: int = 0,
) -> str:
    """Render only explicit source facts and clearly marked unknowns.

    sources: список уникальных источников набора (агрегатор). В пост не
    выводится — источники учитываются только в строке статуса и в БД.

    Формат с «воздухом»: пустые строки между блоками (заголовок / время и
    маршрут / статус / текст источника / бренд) — пост читается с одного
    взгляда в ленте. Заголовок капсом — визуальный якорь.
    """
    when = kyiv_fmt(event_ts)  # киевское время, не UTC сервера

    if fact.weapon_class == "stand_down":
        # Отбой — отдельная короткая ветка, публикуется всегда и мгновенно.
        place = _region_title(fact)
        head = "🟢 ВІДБІЙ" + (f" — {place.upper()}" if place else "") + f" — {when}"
        return "\n\n".join([head, SOURCE_BRAND_LINE])[:4000]

    weapon = WEAPON_SHORT.get(fact.weapon_class, fact.weapon_class)
    # Реактивный подтип: пометка в шапке объясняет, почему ETA короче
    # типового для класса (каскад ниже считает по профилю скорости).
    if getattr(fact, "speed_profile", "") == "reactive":
        weapon = f"{weapon} (реактивний)"
    emoji = _SEVERITY_EMOJI.get(weapon_severity(fact.weapon_class), "⚪")
    place = _region_title(fact)
    from threat_parser import parse_post
    parsed = parse_post(text)
    struct: list[str] = []
    if parsed.kind == "track" and parsed.oblasts:
        # Шапка: где цель СЕЙЧАС (область «звідки» сюда не попадает); тип оружия — отдельной строкой ниже.
        regions, struct = _structured_body(parsed)
        lines = [f"{emoji} {regions.upper()}", weapon, "", f"🕒 {when}"]
        lines.extend(struct)
        if not struct:
            origin = _origin_text(parsed)
            if origin:
                lines.append("↩️ Звідки: " + origin)
    else:
        head = f"{emoji} {place.upper()} | {weapon}" if place else f"{emoji} {weapon}"
        lines = [head, ""]
        lines.append(f"🕒 {when}")
    if parsed.kind == "track" and parsed.oblasts:
        pass  # «Звідки/Курс» уже выведены из наблюдений
    elif fact.origin_region and fact.destination_region and fact.destination_region != "unknown":
        lines.append(
            f"📍 Рух: цілі прямують з {region_name(fact.origin_region)} "
            f"на {region_name(fact.destination_region)}"
        )
    # ETA-каскад шапки: 1) трек по фактическим засечкам (track_speed),
    # 2) расчёт по вектору «з X на Y» (конкретное число минут),
    # 3) типовой диапазон по классу оружия. Правило проекта: ETA можно
    # публиковать только с пометкой «орієнтовно». «—» (нет данных) не
    # показываем.
    from eta import vector_eta_text
    from track_speed import track_eta_text
    trk = track_eta_text(track)
    vec = vector_eta_text(fact)
    if trk:
        lines.append(trk)
    elif vec:
        lines.append(vec)
    else:
        eta = weapon_eta(fact.weapon_class, getattr(fact, "speed_profile", ""))
        if eta and eta != "—":
            lines.append(f"⏱ Типовий підліт: {eta} (орієнтовно)")
    if lines[-1] != "":
        lines.append("")
    status_line = _status(confirmation)
    if status_line:
        lines.append(f"⚠️ Статус: {status_line}")
    if lag_s > 600:  # пост обробили із запізненням — не видаємо за свіже
        lines.append(f"⏳ Повідомлення опубліковано {lag_s // 60} хв тому — інформація може бути застарілою.")
    # Текст источника — недоверенные данные: без разметки, но с сохранением
    # авторских переносов строк (сводки вида «Київщина \n 1 БпЛА на Димер»
    # схлопывать в одну строку нечитаемо). Лимит 1200: полные сводки
    # мониторинга со списком направлений не режутся на полуслове, а лимит
    # поста Telegram (4096) всё равно страхует сверху.
    if struct:
        body = ""   # весь смысл уже в строках «Зараз / Курс»; пересказ источника не нужен
    else:
        body = compact_roundup(text) or "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if body:
        lines.append("")
        lines.append(body[:1200])
    if lines[-1] != "":
        lines.append("")
    lines.append(SOURCE_BRAND_LINE)
    return "\n".join(lines)[:4000]
