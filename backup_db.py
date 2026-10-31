#!/usr/bin/env python3
"""backup_db.py — точка входу для systemd-таймера. Повний backup: airradar/ops/backup.py (див. docs/BACKUP_AND_RECOVERY.md)."""
import sys

from airradar.ops import backup

if __name__ == "__main__":
    sys.exit(backup.main(["backup", "create"]))
