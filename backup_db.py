#!/usr/bin/env python3
"""backup_db.py — щоденна онлайн-копія БД (sqlite backup API, без зупинки бота) у /var/backups/airradar, зберігаємо 5 останніх."""
import gzip
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from config import load_settings

KEEP = 5


def main() -> int:
    src = Path(load_settings().db_path)
    out_dir = Path(os.getenv("BACKUP_DIR", "/var/backups/airradar"))
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "airradar.tmp.db"
    dst = out_dir / f"airradar-{time.strftime('%Y%m%d-%H%M')}.db.gz"
    con = sqlite3.connect(str(src), timeout=60)
    bck = sqlite3.connect(str(tmp))
    with bck:
        con.backup(bck)
    bck.close(); con.close()
    with open(tmp, "rb") as f_in, gzip.open(dst, "wb", compresslevel=3) as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()
    for old in sorted(out_dir.glob("airradar-*.db.gz"))[:-KEEP]:
        old.unlink()
    print(f"ok {dst} {dst.stat().st_size // 1_000_000} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
