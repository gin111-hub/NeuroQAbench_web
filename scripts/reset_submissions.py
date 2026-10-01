#!/usr/bin/env python3
"""Wipe verify_submissions and reset the exports.

Use when:
  * You've been testing the UI and want to throw away your fake submissions
    before real experts start.
  * You want a clean slate.

Default behaviour:
  1. BACKUP the current db to backend/backups/pre-reset-<timestamp>.sqlite.gz
     (so if you reset by mistake, your rows are not gone forever)
  2. DELETE all rows from verify_submissions (schema stays)
  3. VACUUM
  4. Re-run show_submissions.py so data/exports/ is also cleared

Flags:
  --no-backup   skip the safety backup (faster, use only if you really meant it)
  --yes         skip the "are you sure?" prompt
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "backend" / "db.sqlite"
BACKUP_DIR = ROOT / "backend" / "backups"


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM verify_submissions").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the pre-reset backup (dangerous)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="don't ask for confirmation")
    args = ap.parse_args()

    n = _count(DB)
    print(f"current rows in verify_submissions: {n}")
    if n == 0:
        print("nothing to delete; refreshing exports anyway")
    else:
        if not args.yes:
            resp = input(f"delete all {n} rows? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("aborted")
                return 1

        if not args.no_backup:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = BACKUP_DIR / f"pre-reset-{stamp}.sqlite"
            conn = sqlite3.connect(DB)
            try:
                dest = sqlite3.connect(backup)
                with dest:
                    conn.backup(dest)
                dest.close()
            finally:
                conn.close()
            with open(backup, "rb") as f_in, gzip.open(f"{backup}.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            backup.unlink()
            print(f"backup: {backup}.gz")

        conn = sqlite3.connect(DB)
        try:
            conn.execute("DELETE FROM verify_submissions")
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        print(f"deleted {n} rows")

    # refresh the exports so VSCode shows the clean state
    subprocess.run([sys.executable, str(HERE / "show_submissions.py")],
                   check=False, stdout=subprocess.DEVNULL)
    print("exports refreshed → data/exports/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
