#!/usr/bin/env python3
"""Збирає miniapp/map2/shelters.json з data/shelters_ua.clean.tsv.gz (OSM, ODbL).

    python miniapp/map2/tools/build_shelters_json.py

Формат: {"v":1,"src":"...","rows":[[lat,lon,kind,name,street],...]}, kind: 0 = назва вказує на цивільне укриття, 1 = точка без назви (тип невідомий).
Це не військові об'єкти: лише цивільні укриття з відкритих даних. Покриття OSM неповне."""
import gzip
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
# У OSM під «сховище/укриття» потрапляють водосховища, зерносховища, книгосховища, ДОТи, альтанки тощо: для людини в небезпеці це оманливо.
# Тому два рівні довіри: 0 = назва прямо вказує на цивільне укриття; 1 = точка без назви (тип невідомий, показується окремо й сірим).
# «Бункер» (building=bunker) у OSM здебільшого військові/історичні споруди: у цивільний шар не беремо.
CIVIL = re.compile(r"укриття|укрытие|бомбосхов|бомбоубеж|протирадіац|протирадиац|прихисток", re.I)
NOISE = re.compile(r"водосхов|зерносхов|овочесхов|хвостосхов|книг|нафто|газо|мазут|мистецьк|галере|\bдот\b|pillbox|батаре|криївк|навіс|навес|відпочинк|ротонд|закинут|вид на|склад|двт|ввур|киур|лінія|линия|укріпрайон|башн|кемп|стоянк|ночівл|будиночок|ховрах|бесід|стіл|лавоч|лавк|скамій|без укриття|вишка|кабан|грибок|будка|ангар|причал|зонтик|столик|бревен", re.I)


def tier(kind: str, name: str):
    if kind != "укриття" or NOISE.search(name):
        return None
    if CIVIL.search(name):
        return 0
    return 1 if not name.strip() else None


def main() -> int:
    rows = []
    with gzip.open(ROOT / "data" / "shelters_ua.clean.tsv.gz", "rt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 3:
                continue
            name = p[3] if len(p) > 3 else ""
            k = tier(p[2], name)
            if k is None:
                continue
            rows.append([round(float(p[0]), 5), round(float(p[1]), 5), k, name[:80], (p[4] if len(p) > 4 else "")[:60]])
    out = ROOT / "miniapp" / "map2" / "shelters.json"
    out.write_text(json.dumps({"v": 1, "src": "OpenStreetMap contributors (ODbL)", "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(rows)} укриттів → {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
