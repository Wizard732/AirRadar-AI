"""build_gazetteer.py — собирает data/gazetteer_ua.tsv.gz из дампа GeoNames (UA.txt).

Запуск: python build_gazetteer.py data/_geonames_dl/UA.txt
Остаются населённые пункты (feature class P) с кириллическими названиями.
Формат строки: название<TAB>область<TAB>lat<TAB>lon<TAB>население.
Источник данных: GeoNames (CC BY 4.0), https://www.geonames.org/
"""

from __future__ import annotations

import csv
import gzip
import sys

ADMIN1 = {
    "01": "cherkaska", "02": "chernigivska", "03": "chernivetska", "04": "dnipropetrovska",
    "05": "donetska", "06": "ivano_frankivska", "07": "kharkivska", "08": "khersonska",
    "09": "khmelnytska", "10": "kirovohradska", "11": "crimea", "12": "kyivska", "13": "kyivska",
    "14": "luhanska", "15": "lvivska", "16": "mykolaivska", "17": "odeska", "18": "poltavska",
    "19": "rivnenska", "20": "crimea", "21": "sumska", "22": "ternopilska", "23": "vinnytska",
    "24": "volynska", "25": "zakarpatska", "26": "zaporizka", "27": "zhytomyrska",
}
_UK = set("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя'ʼ’-– ")


def is_ukrainian(name: str) -> bool:
    low = name.lower()
    return bool(low) and all(ch in _UK for ch in low) and any(ch in "іїєґ" for ch in low) or (
        bool(low) and all(ch in _UK for ch in low) and len(low) > 3
    )


def build(src: str, dst: str) -> int:
    rows: dict[tuple[str, str, float, float], int] = {}
    with open(src, encoding="utf-8") as fh:
        for r in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(r) < 15 or r[6] != "P":
                continue
            oblast = ADMIN1.get(r[10])
            if not oblast:
                continue
            lat, lon, pop = round(float(r[4]), 4), round(float(r[5]), 4), int(r[14] or 0)
            names = {n.strip() for n in r[3].split(",") if is_ukrainian(n.strip())}
            for name in names:
                rows[(name, oblast, lat, lon)] = pop
    with gzip.open(dst, "wt", encoding="utf-8") as out:
        for (name, oblast, lat, lon), pop in sorted(rows.items()):
            out.write(f"{name}\t{oblast}\t{lat}\t{lon}\t{pop}\n")
    return len(rows)


if __name__ == "__main__":
    n = build(sys.argv[1] if len(sys.argv) > 1 else "data/_geonames_dl/UA.txt", "data/gazetteer_ua.tsv.gz")
    print("записей:", n)
