#!/usr/bin/env python3
"""compare_providers.py — порівняння Ollama і Groq на ОДНАКОВИХ постах: затримка, частка успішних стиснень, відмови.

    python scripts/compare_providers.py [N=50] [ollama,groq]
Потрібні доступні сервіси (Ollama локально / GROQ_API_KEY у .env). Тексти беруться з tests_data/ar_sample.jsonl.gz
(публічні пости каналів; нічого не публікується й не надсилається користувачам). Ключі не друкуються.
Висновок про перенос парсингу робіть лише за цими вимірами + власною оцінкою якості на вибірці.
"""
import asyncio
import gzip
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from ai_summarizer import _build_inner  # noqa: E402
from config import load_settings  # noqa: E402


def sample(n):
    out = []
    with gzip.open(Path(__file__).resolve().parent.parent / "tests_data" / "ar_sample.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            t = (json.loads(line).get("text") or "").strip()
            if 30 < len(t) < 1500:
                out.append(t)
            if len(out) >= n:
                break
    return out


async def main(n, backends):
    s = load_settings()
    posts = sample(n)
    async with aiohttp.ClientSession() as session:
        for b in backends:
            try:
                prov = _build_inner(b, s, session)
            except Exception as exc:  # noqa: BLE001
                print(f"{b}: недоступний ({type(exc).__name__})"); continue
            lat, changed, fail = [], 0, 0
            for t in posts:
                t0 = time.perf_counter()
                try:
                    r = await asyncio.wait_for(prov.summarize(t), 60)
                except Exception:  # noqa: BLE001
                    fail += 1; continue
                lat.append((time.perf_counter() - t0) * 1000)
                changed += r.strip() != t.strip()
            if not lat:
                print(f"{b}: усі {len(posts)} запитів невдалі"); continue
            lat.sort()
            print(f"{b:8} n={len(posts)} стиснено={changed} (без змін/деградація={len(lat) - changed}) відмов={fail} "
                  f"p50={statistics.median(lat):.0f}мс p95={lat[int(.95 * (len(lat) - 1))]:.0f}мс")
            await prov.aclose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 50, (sys.argv[2] if len(sys.argv) > 2 else "ollama,groq").split(",")))
