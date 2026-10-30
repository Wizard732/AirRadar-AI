"""eval_parser.py — прогон threat_parser по реальным постам (tests_data/).

Показывает: распределение видов постов, долю сводок, геокодинг, список
нераспознанных населённых пунктов (что добавлять в справочник) и подозрительные
случаи. Запуск: python eval_parser.py [--show kind] [--n 15]
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import random

from threat_parser import parse_post


def load(path: str = "tests_data/ar_sample.jsonl.gz") -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", default="")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--places", type=int, default=40)
    args = ap.parse_args()
    rows = load()
    kinds: collections.Counter[str] = collections.Counter()
    precision: collections.Counter[str] = collections.Counter()
    unresolved: collections.Counter[str] = collections.Counter()
    multi = 0
    shown: list[tuple[dict, object]] = []
    for r in rows:
        p = parse_post(r["text"], r["source"])
        kinds[p.kind] += 1
        if len(p.oblasts) >= 2:
            multi += 1
        for o in p.observations:
            precision[o.precision] += 1
            if o.place and o.precision != "place":
                unresolved[f"{o.place} [{o.oblast}]"] += 1
        if args.show and p.kind == args.show:
            shown.append((r, p))
    print("posts", len(rows))
    print("kinds", dict(kinds.most_common()))
    print("multi-oblast posts (old pipeline kept 1 region):", multi)
    print("observation precision", dict(precision))
    print("top unresolved places:")
    for name, cnt in unresolved.most_common(args.places):
        print(f"  {cnt:5d} {name}")
    random.seed(3)
    for r, p in random.sample(shown, min(args.n, len(shown))):
        print("-----", r["source"], r["weapon_class"], r["region"])
        print(r["text"][:350])
        print("=>", p.kind, p.reasons, [(o.weapon, o.count, o.oblast, o.place, o.relation, o.precision) for o in p.observations][:8])


if __name__ == "__main__":
    main()
