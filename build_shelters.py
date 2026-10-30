#!/usr/bin/env python3
"""build_shelters.py — локальний набір укриттів з вивантаження OSM (Overpass) → data/shelters_ua.tsv.gz.

    python build_shelters.py ua.json

Бере amenity=shelter (без павільйонів/навісів/зупинок), building=bunker, emergency=shelter і об'єкти з «укриття/сховище» в назві.
Дані © OpenStreetMap contributors (ODbL). Покриття неповне; це не офіційний реєстр захисних споруд.
"""
import gzip
import json
import re
import sys
from pathlib import Path

BAD_TYPE = {"pavilion", "sun_shelter", "public_transport", "basic_hut", "picnic_shelter", "weather_shelter", "rock_shelter",
            "lean_to", "gazebo", "field_shelter", "bus_stop", "tram_stop", "roof"}
NAME_RE = re.compile(r"укриття|сховище|бомбосховище", re.I)
BUS_RE = re.compile(r"зупин|bus|трамв|тролей|павільйон|навіс|альтан|беседк", re.I)


def classify(t: dict) -> str:
    name = t.get("name", "") or t.get("name:uk", "")
    if BUS_RE.search(name) or t.get("public_transport") or t.get("highway") == "bus_stop":
        return ""
    if t.get("building") == "bunker" or t.get("military") == "bunker" or t.get("amenity") == "bunker":
        return "бункер"
    if t.get("emergency") == "shelter" or NAME_RE.search(name):
        return "укриття"
    if t.get("amenity") == "shelter":
        st = t.get("shelter_type", "")
        if st in BAD_TYPE or t.get("building") in ("roof",) or st == "pavilion":
            return ""
        return "укриття"
    return ""


def main() -> int:
    src = Path(sys.argv[1])
    els = json.loads(src.read_text(encoding="utf-8"))["elements"]
    rows, seen = [], set()
    for e in els:
        c = e if "lat" in e else e.get("center") or {}
        if "lat" not in c:
            continue
        t = e.get("tags", {})
        kind = classify(t)
        if not kind:
            continue
        key = (round(c["lat"], 4), round(c["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        name = (t.get("name") or t.get("name:uk") or "").replace("\t", " ").replace("\n", " ")[:80]
        street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x).replace("\t", " ")[:60]
        rows.append((round(c["lat"], 5), round(c["lon"], 5), kind, name, street))
    out = Path(__file__).parent / "data" / "shelters_ua.tsv.gz"
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join(map(str, r)) + "\n")
    print(f"{len(rows)} укриттів → {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
