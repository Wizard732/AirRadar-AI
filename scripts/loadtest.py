#!/usr/bin/env python3
"""loadtest.py — вимірювання на ТИМЧАСОВІЙ БД (прод не чіпає): черга, trace, метрики, дашборд, паралельний доступ.

    python scripts/loadtest.py [N]   (N — кількість подій; за замовчуванням 5000)
Виводить фактичні виміри; висновки про оптимізацію робити лише за ними.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airradar.delivery import notify_queue  # noqa: E402
from airradar.monitoring import dashboard, incident_trace, metrics  # noqa: E402
from airradar.ops import migrations  # noqa: E402
from database import Database  # noqa: E402


def timed(label, fn):
    t = time.perf_counter(); r = fn(); dt = time.perf_counter() - t
    print(f"{label:<44}{dt * 1000:10.1f} мс"); return r


def main(n: int) -> None:
    db = Database(str(Path(tempfile.mkdtemp()) / "load.db"))
    migrations.apply_pending(db)
    users = max(10, n // 10)
    timed(f"enqueue {n} сповіщень ({users} користувачів)", lambda: [notify_queue.enqueue(db, f"ev{i % 200}", i % users, "tg") for i in range(n)])
    timed(f"trace {n} кроків", lambda: [incident_trace.record(db, f"ev{i % 500}", "received", data={"text": "x" * 200}) for i in range(n)])
    timed(f"metrics {n} замірів", lambda: [metrics.record_stage(db, "total", i % 900) for i in range(n)])
    timed("claim_due(500)", lambda: notify_queue.claim_due(db, 500))
    timed("dashboard.collect(24h)", lambda: dashboard.collect(db, "24h"))
    timed("timeline(1 подія)", lambda: incident_trace.timeline(db, "ev1"))
    errs = []

    def worker():
        try:
            for i in range(200):
                notify_queue.enqueue(db, f"par{threading.get_ident()}{i}", i, "tg")
                dashboard.collect(db, "1h")
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))
    ts = [threading.Thread(target=worker) for _ in range(8)]
    timed("8 потоків × 200 (запис+читання)", lambda: ([t.start() for t in ts], [t.join() for t in ts]))
    print("помилки паралельного доступу:", errs or "немає")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
