"""eval_independence.py — перевірка незалежності джерел на реальних постах (tests_data).

Для кожного вікна (за замовчуванням 10 хв) групує схожі пости (trust.RepostClusters). Показує, скільки «підтверджень
двома каналами» насправді є пересказами одного тексту. Запуск: python eval_independence.py [--window 600] [--limit N]
"""
from __future__ import annotations

import argparse
import collections

from eval_parser import load
from trust import RepostClusters


def run(rows: list[dict], window_s: int = 600) -> dict:
    rows = sorted(rows, key=lambda r: r["event_ts"])
    cluster_sizes: collections.Counter[int] = collections.Counter()
    multi_source = fake_confirmed = 0
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j]["event_ts"] - rows[i]["event_ts"] <= window_s:
            j += 1
        rc = RepostClusters()
        by_cluster: dict[int, set[str]] = collections.defaultdict(set)
        for r in rows[i:j]:
            by_cluster[rc.add(r["source"], r["text"])].add(r["source"])
        for srcs in by_cluster.values():
            cluster_sizes[len(srcs)] += 1
        sources = {r["source"] for r in rows[i:j]}
        if len(sources) >= 2:
            multi_source += 1
            if rc.independent_count() == 1:
                fake_confirmed += 1   # ≥2 каналів, але це один і той самий текст
        i = j
    return {
        "posts": len(rows), "windows_with_2plus_sources": multi_source,
        "windows_where_all_posts_are_reposts": fake_confirmed,
        "repost_share": round(fake_confirmed / multi_source, 4) if multi_source else 0.0,
        "cluster_size_histogram": dict(sorted(cluster_sizes.items())),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=600)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    data = load()
    print(run(data[: a.limit] if a.limit else data, a.window))
