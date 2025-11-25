#!/usr/bin/env python3
"""
Best-effort salvage of historical price data from a quarantined (possibly malformed)
SQLite database into a fresh database using the current schema.

Usage:
  python scripts/salvage_history.py --source data/history.db.corrupt-YYYYMMDD-HHMMSS --dest data/history.db

Notes:
- Reads rows in ascending id order and inserts them via PriceHistory.record_price,
  skipping rows that raise decode/DB errors.
- If a full-table scan fails, it falls back to per-row reads by id to salvage what it can.
- Exchange rates are copied best-effort as well.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional  # noqa: F401  (future use)

from sale_monitor.storage.price_history import PriceHistory  # type: ignore


def integrity_ok(path: str) -> bool:
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row and row[0] == "ok")
    except sqlite3.DatabaseError:
        return False


def ensure_dest_schema(dest: str) -> PriceHistory:
    ph = PriceHistory(dest)
    return ph


def try_copy_exchange_rates(src_conn: sqlite3.Connection, dest_conn: sqlite3.Connection) -> int:
    copied = 0
    try:
        cur = src_conn.execute(
            """
            SELECT base_currency, target_currency, rate, timestamp
            FROM exchange_rates
            """
        )
        rows = cur.fetchall()
        if rows:
            dest_conn.executemany(
                """
                INSERT OR REPLACE INTO exchange_rates
                (base_currency, target_currency, rate, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            dest_conn.commit()
            copied = len(rows)
    except sqlite3.Error:
        pass
    return copied


def copy_price_history_bulk(src_conn: sqlite3.Connection, dest_ph: PriceHistory) -> int:
    """Fast path: try a simple ordered scan; raise on failure so caller can fall back."""
    total = 0
    cur = src_conn.execute(
        """
        SELECT id, product_url, product_name, price, timestamp, check_status, currency, price_cad
        FROM price_history
        ORDER BY id ASC
        """
    )
    for row in cur:
        _id, url, name, price, ts, status, currency, price_cad = row
        try:
            dest_ph.record_price(url, name, float(price), ts, status or "success", currency or "CAD", price_cad)
            total += 1
        except (sqlite3.Error, ValueError, TypeError):
            # skip bad row
            continue
    return total


def copy_price_history_rowwise(src_conn: sqlite3.Connection, dest_ph: PriceHistory) -> int:
    """Slower path: read ids then pull rows one-by-one, skipping any that error."""
    total = 0
    try:
        rng = src_conn.execute("SELECT MIN(id), MAX(id) FROM price_history").fetchone()
        if not rng or rng[0] is None or rng[1] is None:
            return 0
        lo, hi = int(rng[0]), int(rng[1])
        for i in range(lo, hi + 1):
            try:
                row = src_conn.execute(
                    """
                    SELECT product_url, product_name, price, timestamp, check_status, currency, price_cad
                    FROM price_history WHERE id = ?
                    """,
                    (i,),
                ).fetchone()
                if not row:
                    continue
                url, name, price, ts, status, currency, price_cad = row
                dest_ph.record_price(url, name, float(price), ts, status or "success", currency or "CAD", price_cad)
                total += 1
            except (sqlite3.Error, ValueError, TypeError):
                continue
    except sqlite3.Error:
        return 0
    return total


def main():
    ap = argparse.ArgumentParser(description="Salvage historical data from a quarantined SQLite DB")
    ap.add_argument("--source", required=True, help="Path to the quarantined/corrupt DB (read-only)")
    ap.add_argument("--dest", required=True, help="Path to the destination DB (current app DB)")
    args = ap.parse_args()

    src = Path(args.source)
    dest = Path(args.dest)

    if not src.exists():
        print(f"[!] Source DB not found: {src}")
        return 2
    if not dest.exists():
        print(f"[*] Destination DB does not exist; creating schema: {dest}")
    dest_ph = ensure_dest_schema(str(dest))

    try:
        with sqlite3.connect(str(src)) as src_conn:
            total = 0
            try:
                total = copy_price_history_bulk(src_conn, dest_ph)
            except sqlite3.Error:
                total = copy_price_history_rowwise(src_conn, dest_ph)
            with sqlite3.connect(str(dest)) as dest_conn:
                xr = try_copy_exchange_rates(src_conn, dest_conn)
            print(f"[*] Salvaged {total} history rows; copied {xr} exchange-rate rows.")
            if not integrity_ok(str(dest)):
                print("[!] Destination DB integrity_check failed after salvage. Data may be partial.")
            else:
                print("[+] Destination DB integrity ok.")
    except sqlite3.DatabaseError as e:
        print(f"[!] Unable to open source DB: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
