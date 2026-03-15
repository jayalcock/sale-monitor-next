#!/usr/bin/env python3
"""One-time backfill: populate NULL price_cad values in price_history.

For each successful record where price_cad IS NULL and currency != base_currency,
this script fetches the current exchange rate and computes the base-currency
equivalent.  Because historical rates aren't available from the free API, every
backfilled row gets the *current* rate — but going forward new checks will store
the rate that was live at check time.

Usage:
    python scripts/backfill_price_cad.py [--db data/history.db] [--config data/config.json] [--dry-run]
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.storage.price_history import PriceHistory


def backfill(db_path: str, config_path: str, dry_run: bool = False):
    base_currency = get_base_currency(config_path).upper()
    history = PriceHistory(db_path)
    ex_service = ExchangeRateService(cache_handler=history)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT rowid, price, currency
            FROM price_history
            WHERE check_status = 'success'
              AND price IS NOT NULL
              AND price_cad IS NULL
              AND currency IS NOT NULL
              AND UPPER(currency) != ?
            """,
            (base_currency,),
        ).fetchall()

    print(f"Found {len(rows)} records with NULL price_cad (base_currency={base_currency})")

    if not rows:
        return

    # Pre-fetch rates for each unique currency
    currencies = {row[2].upper() for row in rows}
    rates = {}
    for cur in currencies:
        rate = ex_service.get_rate(cur, base_currency)
        if rate is not None:
            rates[cur] = rate
            print(f"  {cur} -> {base_currency}: {rate}")
        else:
            print(f"  WARNING: no rate for {cur} -> {base_currency}")

    updated = 0
    skipped = 0
    with sqlite3.connect(db_path) as conn:
        for rowid, price, currency in rows:
            cur = currency.upper()
            rate = rates.get(cur)
            if rate is None:
                skipped += 1
                continue
            price_cad = round(float(price) * rate, 2)
            if not dry_run:
                conn.execute(
                    "UPDATE price_history SET price_cad = ? WHERE rowid = ?",
                    (price_cad, rowid),
                )
            updated += 1
        if not dry_run:
            conn.commit()

    label = "(dry run) " if dry_run else ""
    print(f"{label}Updated {updated} records, skipped {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill NULL price_cad in history DB")
    parser.add_argument("--db", default=os.getenv("HISTORY_DB", "data/history.db"))
    parser.add_argument("--config", default=os.getenv("CONFIG_FILE", "data/config.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(args.db, args.config, args.dry_run)
