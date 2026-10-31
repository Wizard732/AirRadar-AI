"""public_view.py — безпечне публічне подання карти: район/область замість live-точок.

Приватний payload (`health_server.radar_payload`) містить точні координати, історію точок, швидкість, курс,
прогноз і ціль. Публічне подання їх не містить: об'єкти групуються за (район, категорія), позиція — фіксований
центроїд району, час округлений до 5 хв, кількість і число джерел — діапазонами; ключ елемента — район+категорія
(не ідентифікатор окремої цілі).

Модуль чистий: без БД і мережі (лише `raion_geo` для «точка → район»).
"""
from __future__ import annotations

import hashlib
from typing import Any

from airradar.safety import object_lifecycle as lc

SCHEMA = 1
BUCKET_S = 300
DEFAULT_DELAY_MIN = 10
NOTICE = "Положення показано узагальнено за районом; точні координати об'єктів не публікуються."

# Ключі, яких не повинно бути в чистому публічному контракті (/api/public/v1/regions).
FORBIDDEN_KEYS = frozenset({
    "points", "now", "speed_kmh", "speed_source", "heading", "heading_text", "forecast", "dest",
    "track", "origin", "destination", "lat", "lon", "id",
})
# Приховані від публіки поля сумісного формату (/api/tracks, tracks.json) — завжди порожні.
COMPAT_EMPTY = ("speed_kmh", "heading", "dest")

_STATUS_RANK = {"stale": 0, "lost": 0, "tracking": 1, "confirmed": 2}
_CENTROIDS: dict[str, tuple[float, float]] = {}


def category_of(weapon: str) -> str:
    """drone | missile | other — за нормалізованим типом, не за «рішенням ШІ»."""
    cls = lc.class_of(weapon)
    return cls if cls in ("drone", "missile") else "other"


def round_ts(ts: int | float, step: int = BUCKET_S) -> int:
    return int(ts) // step * step


def bucket_count(n: int | None) -> str:
    n = int(n or 1)
    return "1" if n <= 1 else ("2-5" if n <= 5 else "6+")


def bucket_sources(n: int | None) -> str:
    n = int(n or 1)
    return "1" if n <= 1 else ("2" if n == 2 else "3+")


def _count_floor(bucket: str) -> int:
    return {"1": 1, "2-5": 2, "6+": 6}[bucket]


def raion_centroid(key: str) -> tuple[float, float] | None:
    """Фіксована точка району (середнє вершин найбільшого контуру): не залежить від live-даних."""
    if key in _CENTROIDS:
        return _CENTROIDS[key]
    import raion_geo
    for k, _bbox, polys in raion_geo._raions():  # noqa: SLF001
        if k == key:
            ring = max((p[0] for p in polys), key=len)
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
            _CENTROIDS[key] = (round(lat, 2), round(lon, 2))
            return _CENTROIDS[key]
    return None


def _oblast_slug(oblast_word: str) -> str | None:
    import regions
    for slug in regions.all_region_slugs():
        if regions.region_name(slug).split(" ")[0] == oblast_word:
            return slug
    return "kyivska" if oblast_word == "Київ" else None


def _status_of(track_status: str, sources: int) -> str:
    if track_status == "stale":
        return "stale"
    return "confirmed" if sources >= 2 else "tracking"


def _gid(key: str, category: str) -> str:
    """Ключ UI — адміністративний район + категорія, а не ідентифікатор окремої цілі."""
    return hashlib.sha1(f"{key}|{category}".encode()).hexdigest()[:10]


def _group(items: list[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    import raion_geo
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for it in items:
        hit = raion_geo.raion_at(it["lat"], it["lon"])
        if not hit:
            continue          # точка поза районами: публічно не показуємо, щоб не вгадувати
        key, cat = hit[0], category_of(it["weapon"])
        g = groups.setdefault((key, cat), {"weapon": it["weapon"], "count": 0, "sources": 1, "status": "stale",
                                           "first": it["first"], "last": it["last"]})
        g["count"] += int(it.get("count") or 1)
        g["sources"] = max(g["sources"], int(it.get("sources") or 1))
        g["first"], g["last"] = min(g["first"], it["first"]), max(g["last"], it["last"])
        st = it["status"]
        if _STATUS_RANK.get(st, 0) >= _STATUS_RANK.get(g["status"], 0):
            g["status"] = st
    return groups


def build_public(private: dict, now: int, delay_min: int = DEFAULT_DELAY_MIN) -> dict:
    """Публічний аналог radar_payload. Сумісний формат tracks/events + чистий контракт `regions`."""
    track_items = []
    for t in private.get("tracks") or []:
        pts = t.get("points") or []
        cur = t.get("now") or {}
        if cur.get("lat") is None:
            continue
        src = int(t.get("sources") or 1)
        track_items.append({
            "weapon": t.get("weapon", ""), "lat": cur["lat"], "lon": cur["lon"], "count": t.get("count") or 1,
            "sources": src, "status": _status_of(t.get("status", "active"), src),
            "first": int(pts[0]["ts"]) if pts else int(private.get("now", now)),
            "last": int(pts[-1]["ts"]) if pts else int(private.get("now", now)),
        })
    event_items = []
    for e in private.get("events") or []:
        if e.get("lat") is None:
            continue
        event_items.append({"weapon": e.get("weapon", ""), "lat": e["lat"], "lon": e["lon"], "count": e.get("count") or 1,
                            "sources": 1, "status": "tracking", "first": int(e["ts"]), "last": int(e["ts"])})

    tracks_out, regions, events_out = [], [], []
    for (key, cat), g in sorted(_group(track_items).items()):
        c = raion_centroid(key)
        if c is None:
            continue
        raion, oblast = key.split("|")
        last_r, first_r = round_ts(g["last"]), round_ts(g["first"])
        cb, sb = bucket_count(g["count"]), bucket_sources(g["sources"])
        slug = _oblast_slug(oblast)
        regions.append({"region": slug, "raion": raion, "category": cat, "status": g["status"], "count_bucket": cb,
                        "sources_bucket": sb, "first_seen": first_r, "last_update": last_r, "stale": g["status"] == "stale"})
        tracks_out.append({
            "id": _gid(key, cat), "weapon": g["weapon"], "profile": "",
            "status": "stale" if g["status"] == "stale" else "active", "count": _count_floor(cb), "count_bucket": cb,
            "sources": int(sb.rstrip("+")), "sources_bucket": sb, "official": None,
            "speed_kmh": None, "speed_source": "none", "heading": None, "heading_text": "",
            "now": {"lat": c[0], "lon": c[1], "estimated": True},
            "points": [{"ts": last_r, "lat": c[0], "lon": c[1], "count": _count_floor(cb), "place": raion, "precision": "region"}],
            "forecast": [], "dest": None, "raion_key": key, "category": cat, "region": slug,
        })
    for (key, cat), g in sorted(_group(event_items).items()):
        c = raion_centroid(key)
        if c is None:
            continue
        raion, oblast = key.split("|")
        events_out.append({"weapon": g["weapon"], "lat": c[0], "lon": c[1], "ts": round_ts(g["last"]),
                           "count": _count_floor(bucket_count(g["count"])), "count_bucket": bucket_count(g["count"]),
                           "place": raion, "oblast": oblast, "precision": "region", "toward": False,
                           "raion_key": key, "category": cat, "region": _oblast_slug(oblast)})

    out = {k: private.get(k) for k in ("sirens", "sirens_raions", "sirens_raions_y", "sirens_raions_p",
                                       "siren_threats", "alerts", "feed", "accuracy", "impacts")}
    seen_feed: set = set()
    out["feed"] = []
    for f in private.get("feed") or []:
        row = {"ts": round_ts(f.get("ts", 0)), "text": public_threat_text(f.get("type", ""), f.get("region", "")),
               "region": f.get("region"), "type": f.get("type")}
        key = (row["ts"], row["text"])
        if key not in seen_feed:
            seen_feed.add(key)
            out["feed"].append(row)
    out["impacts"] = public_impacts({"now": private.get("now", now), "items": private.get("impacts") or []})["items"]
    out.update({"schema": SCHEMA, "public": True, "notice": NOTICE, "delay_min": delay_min,
                "now": round_ts(private.get("now", now)), "updated_at": round_ts(private.get("now", now)),
                "window_min": private.get("window_min"), "tracks": tracks_out, "events": events_out, "regions": regions})
    return out


def regions_contract(public: dict) -> dict:
    """Чистий контракт `/api/public/v1/regions`: без координат і службових полів сумісного формату."""
    return {"schema": SCHEMA, "updated_at": public["updated_at"], "delay_min": public["delay_min"],
            "notice": NOTICE, "regions": public["regions"],
            "official": {k: public.get(k) for k in ("sirens", "sirens_raions", "sirens_raions_y", "sirens_raions_p",
                                                    "siren_threats")}}


_TYPE_LABEL = {
    "shahed": "БпЛА", "uav": "БпЛА", "fpv": "БпЛА", "recon_drone": "Розвідувальний БпЛА",
    "ballistic": "Балістика", "cruise_missile": "Крилаті ракети", "missile": "Ракети", "air_missile": "Ракети",
    "coastal_missile": "Ракети", "kab": "КАБ", "aviation": "Авіація", "tac_aviation": "Авіація",
    "strat_aviation": "Стратегічна авіація", "mlrs": "Обстріл РСЗО", "artillery": "Обстріл", "explosion": "Вибухи",
    "air_defense": "Робота ППО", "alert": "Повітряна тривога", "stand_down": "Відбій",
}


def public_threat_text(type_: str, region: str) -> str:
    """Загальний текст без міст, адрес, курсу й об'єктів: «Тип — область». Вихідний текст каналів містить місця
    (напр. «Суми, Низи», «курс на …», «вул. …»), тому публічно використовується лише нормалізований тип і область."""
    import regions
    try:
        name = regions.region_name(region) if region else ""
    except Exception:  # noqa: BLE001
        name = ""
    label = _TYPE_LABEL.get(type_, "Загроза")
    return f"{label} — {name}" if name else label


def strip_threat(item: dict) -> dict:
    """/api/threats: без вектора (курс, швидкість, from/to), напрямку, міст у тексті; час — до 5 хв, джерела — до «3+»."""
    out = {k: v for k, v in item.items() if k not in ("track", "origin", "destination")}
    out["text"] = public_threat_text(item.get("type", ""), item.get("region", ""))
    if "ts" in out:
        out["ts"] = round_ts(out["ts"])
    if "age_min" in out:
        out["age_min"] = int(out["age_min"]) // 5 * 5
    if "sources" in out:
        out["sources"] = min(int(out["sources"] or 1), 3)
    return out


def coarsen_report(report: dict) -> dict | None:
    """Репорт користувача: точка → центроїд району (точні координати користувача не публікуються)."""
    import raion_geo
    if report.get("lat") is None or report.get("lon") is None:
        return None
    hit = raion_geo.raion_at(report["lat"], report["lon"])
    c = raion_centroid(hit[0]) if hit else None
    if c is None:
        return None
    rest = {k: v for k, v in report.items() if k not in ("lat", "lon", "ts", "text", "age_min")}
    return {**rest, "ts": round_ts(report.get("ts", 0)), "age_min": int(report.get("age_min", 0)) // 5 * 5,
            "text": "Повідомлення користувача", "lat": c[0], "lon": c[1]}   # вільний текст користувача не публікується


IMPACT_WINDOW_S = 3 * 3600
IMPACT_DELAY_MIN = 180


def _range_label(n: int) -> str:
    """Діапазон замість точного числа жертв: 0, 1-2, 3-5, 6-10, 10+."""
    return "0" if n <= 0 else ("1-2" if n <= 2 else ("3-5" if n <= 5 else ("6-10" if n <= 10 else "10+")))


def _hits_label(n: int) -> str:
    return "1" if n <= 1 else ("2-3" if n <= 3 else "4+")


def public_impacts(payload: dict, delay_min: int = IMPACT_DELAY_MIN) -> dict:
    """Безпечний варіант impacts.json: агрегація за областю та 3-годинним вікном, діапазони замість точних чисел.

    Без міста/координат/точного часу; записи свіжіші за delay_min не публікуються; для вікна з єдиним попаданням
    замість діапазонів — лише «0» або «1+» (одиничну подію не відновити). Підсумки округлені до 10, щоб різниця
    між оновленнями не видавала точні числа окремих записів (див. docs/impacts-audit.md)."""
    from kyiv_time import fmt as kyiv_fmt
    cutoff = int(payload.get("now", 0)) - delay_min * 60
    groups: dict[tuple[str, int, str], dict] = {}
    for it in payload.get("items", []):
        if it.get("ts", 0) > cutoff:
            continue
        w = int(it["ts"]) // IMPACT_WINDOW_S * IMPACT_WINDOW_S
        g = groups.setdefault((it.get("slug", ""), w, it.get("kind", "hit")),
                              {"oblast": it.get("oblast", ""), "hits": 0, "killed": 0, "injured": 0, "children": 0,
                               "damage": set(), "unclear": 0})
        g["hits"] += 1
        g["killed"] += int(it.get("killed") or 0)
        g["injured"] += int(it.get("injured") or 0)
        g["children"] += int(it.get("children") or 0)
        g["damage"].update(it.get("damage") or [])
        g["unclear"] += 1 if it.get("flags") else 0
    items = []
    totals = {"hits": 0, "killed": 0, "injured": 0, "children": 0, "unclear": 0}
    for (slug, w, kind), g in sorted(groups.items(), key=lambda kv: -kv[0][1]):
        single = g["hits"] == 1
        kr = ("0" if g["killed"] <= 0 else "1+") if single else _range_label(g["killed"])
        ir = ("0" if g["injured"] <= 0 else "1+") if single else _range_label(g["injured"])
        items.append({"ts": w, "when": f"{kyiv_fmt(w, '%d.%m %H:%M')}–{kyiv_fmt(w + IMPACT_WINDOW_S, '%H:%M')}",
                      "oblast": g["oblast"], "slug": slug, "place": "", "lat": None, "lon": None, "precision": "oblast",
                      "kind": kind, "hits_label": _hits_label(g["hits"]), "killed_range": kr, "injured_range": ir,
                      "damage": sorted(g["damage"]), "flags": "", "window_h": IMPACT_WINDOW_S // 3600})
        if kind == "hit":
            totals["hits"] += g["hits"]
            for k in ("killed", "injured", "children", "unclear"):
                totals[k] += g[k]
    for k in ("killed", "injured", "children"):
        totals[k] = totals[k] // 10 * 10
    totals["unclear"] = 0
    out = {k: v for k, v in payload.items() if k not in ("items", "totals")}
    out.update({"now": round_ts(payload.get("now", 0)), "delay_min": delay_min, "items": items, "totals": totals,
                "note": "Дані зі зведень каналів: неофіційні, можуть уточнюватись. Область і 3-годинне вікно; кількість жертв — "
                        "діапазонами, підсумки округлені до 10; міста й адреси не публікуються."})
    return out


_RISK_TEXT = {"over": "Загроза над вашим районом: негайно до укриття.",
              "imminent": "Загроза поблизу: будьте готові піти в укриття.",
              "watch": "Підвищена увага в вашому районі."}


def public_risk(risks: list[dict]) -> list[dict]:
    """/api/risk: лише рівень і загальний текст — без відстані, пеленга, ETA і швидкості (інакше це оракул позиції)."""
    order = {"watch": 1, "imminent": 2, "over": 3}
    top = max(risks, key=lambda r: order.get(r.get("level"), 0), default=None)
    if top is None:
        return []
    lvl = top.get("level", "watch")
    return [{"level": lvl, "text": _RISK_TEXT.get(lvl, "Підвищена увага.")}]
