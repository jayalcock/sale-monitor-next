#!/usr/bin/env python3
"""Backfill missing CAD-converted prices in price_history.

Usage:
  PYTHONPATH=src python scripts/backfill_history_price_cad.py \
    --db data/history.db --dry-run

What it does:
  * Finds rows where currency != 'CAD' AND (price_cad IS NULL OR price_cad = 0)
  * Converts price -> CAD using ExchangeRateService (cached rates honored)
  * Updates price_cad field (rounded to 2 decimals)
  * Reports summary JSON to stdout

Safe to re-run; only fills rows that still lack price_cad.
"""
import argparse
import json
import sqlite3
from typing import List, Tuple

from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.services.exchange_rates import ExchangeRateService


def fetch_incomplete_rows(conn: sqlite3.Connection) -> List[Tuple[int, float, str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rowid, price, currency
        FROM price_history
        WHERE (price_cad IS NULL OR price_cad = 0)
          AND UPPER(currency) != 'CAD'
        """
    )
    return cur.fetchall()


def backfill(db_path: str, dry_run: bool = False) -> dict:
    ph = PriceHistory(db_path)
    ex_service = ExchangeRateService(cache_handler=ph)

    conn = sqlite3.connect(db_path)
    rows = fetch_incomplete_rows(conn)
    updated = 0
    failed = 0

    for rowid, price, currency in rows:
        if price is None or currency is None:
            failed += 1
            continue
        cur_code = currency.upper()
        try:
            converted = ex_service.convert(float(price), cur_code, 'CAD')
        except Exception:
            converted = None
        if converted is None:
            failed += 1
            continue
        if not dry_run:
            conn.execute(
                "UPDATE price_history SET price_cad = ? WHERE rowid = ?",
                (round(converted, 2), rowid)
            )
        updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return {
        'rows_considered': len(rows),
        'rows_updated': updated,
        'rows_failed': failed,
        'dry_run': dry_run
    }


def main():
    ap = argparse.ArgumentParser(description='Backfill missing CAD conversions in price_history table.')
    ap.add_argument('--db', default='data/history.db', help='Path to history.db')
    ap.add_argument('--dry-run', action='store_true', help='Do not write changes; just report what would happen.')
    args = ap.parse_args()

    result = backfill(args.db, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
