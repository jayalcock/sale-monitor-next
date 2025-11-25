#!/usr/bin/env python3
"""
Safely restore the history SQLite database from a SQL dump.

Usage:
  python scripts/restore_history_db.py --dump data/history_dump.sql.gz --dest data/history.db

It will:
- create a timestamped backup of the current dest DB (if any)
- import the SQL dump into a fresh DB
- run VACUUM and integrity_check
- print a summary of rows restored
"""
import argparse
import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def restore(dump_path: str, dest_path: str):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing
    if dest.exists():
        ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
        backup = dest.parent / f"{dest.name}.backup-{ts}"
        shutil.copy2(dest, backup)
        for s in ("-wal", "-shm"):
            side = Path(str(dest) + s)
            if side.exists():
                shutil.copy2(side, Path(str(backup) + s))
        print(f"[i] Backed up existing DB to {backup}")

    # Remove target and side files
    for p in [dest, Path(str(dest)+"-wal"), Path(str(dest)+"-shm")]:
        if p.exists():
            p.unlink()

    # Import
    print(f"[i] Importing dump {dump_path} -> {dest_path}")
    with gzip.open(dump_path, 'rt', encoding='utf-8') as f:
        sql = f.read()
    with sqlite3.connect(dest_path) as conn:
        conn.executescript(sql)
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        conn.commit()
        # Verify
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'
        except sqlite3.Error:
            ok = False
        rows = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    print(f"[+] Restored rows: {rows}; integrity_ok={ok}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dump', required=True, help='Path to .sql.gz dump')
    ap.add_argument('--dest', required=True, help='Path to destination DB')
    args = ap.parse_args()
    restore(args.dump, args.dest)

if __name__ == '__main__':
    main()
