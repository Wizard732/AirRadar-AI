"""benchmark.py — AirRadar AI Benchmark: фіксований датасет → метрики парсера й безпеки.

Запуск: python benchmark.py [--path tests_data/ar_sample.jsonl.gz] [--limit N] [--json]
Окремо рахується частка ХИБНИХ КРИТИЧНИХ (публічний пост про «imminent» там, де еталон каже інакше),
бо для безпеки загальний F1 не відображає ризику. Еталон — поля weapon_class/stage/region у датасеті.
"""
from __future__ import annotations

import argparse
import json
import time

from eval_parser import load
from safety_policy import check_publication
from threat_parser import parse_post


# Еталонні стадії активної загрози в датасеті; решта (all_clear/alert/impact/intercept) — не «борт у повітрі».
ACTIVE_STAGES = {"movement", "launch"}


def run(rows: list[dict]) -> dict:
    tp = fp = fn = 0
    region_ok = region_n = 0
    false_critical = critical_total = 0
    violations = 0
    clear_ok = clear_n = 0
    t0 = time.perf_counter()
    for r in rows:
        p = parse_post(r["text"], r["source"])
        predicted = p.kind == "track" and bool(p.observations)
        expected = r.get("stage") in ACTIVE_STAGES
        if predicted and expected:
            tp += 1
        elif predicted and not expected:
            fp += 1
        elif expected and not predicted:
            fn += 1
        if predicted:
            critical_total += 1
            if not expected:
                false_critical += 1
        if expected and p.oblasts and r.get("region") and r["region"] != "unknown":
            region_n += 1
            region_ok += int(r["region"] in p.oblasts)
        if r.get("stage") == "all_clear":
            clear_n += 1
            clear_ok += int(p.kind == "all_clear")
        if not check_publication(r["text"]).allowed:
            violations += 1
    elapsed = time.perf_counter() - t0
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {
        "posts": len(rows),
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
        "geographic_accuracy": round(region_ok / region_n, 4) if region_n else None,
        "false_critical_rate": round(false_critical / critical_total, 4) if critical_total else 0.0,
        "all_clear_recall": round(clear_ok / clear_n, 4) if clear_n else None,
        "safety_policy_blocked": violations,
        "mean_latency_ms": round(elapsed * 1000 / max(1, len(rows)), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="tests_data/ar_sample.jsonl.gz")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = load(args.path)
    if args.limit:
        rows = rows[: args.limit]
    res = run(rows)
    print(json.dumps(res, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
