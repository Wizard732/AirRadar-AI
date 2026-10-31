#!/usr/bin/env python3
"""Перевірка gazetteer і укриттів. --write-clean створює data/shelters_ua.clean.tsv.gz (оригінал НЕ змінюється)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from airradar.product import geo_quality as g  # noqa: E402

b = g.Boundaries()
print("gazetteer:", g.check_gazetteer(bounds=b, sample_membership=3000))
r = g.check_shelters(bounds=b)
good = r.pop("_good")
print("shelters:", r)
if "--write-clean" in sys.argv:
    dst = Path(__file__).resolve().parent.parent / "data" / "shelters_ua.clean.tsv.gz"
    print("записано", g.write_clean_shelters(good, dst), "->", dst.name)
