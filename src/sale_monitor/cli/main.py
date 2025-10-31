#!/usr/bin/env python3
"""
Sale Monitor CLI - Command-line interface for the Sale Monitor application.
"""

import argparse
import logging
import os
from datetime import datetime

from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state, save_state

def main() -> int:
    parser = argparse.ArgumentParser(description="Sale Monitor - CSV-backed")
    parser.add_argument("--products-csv", default=os.getenv("PRODUCTS_CSV", "data/products.csv"))
    parser.add_argument("--state-file", default=os.getenv("STATE_FILE", "data/state.json"))
    parser.add_argument("--user-agent", default=os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; SaleMonitor/1.0)"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT", "30")))
    parser.add_argument("--max-retries", type=int, default=int(os.getenv("MAX_RETRIES", "3")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s - %(levelname)s - %(message)s")

    # CSV takes precedence; if missing, fail fast to make it explicit
    products = read_products(args.products_csv)
    state = load_state(args.state_file)

    extractor = PriceExtractor(user_agent=args.user_agent, timeout=args.timeout, max_retries=args.max_retries)

    enabled = [p for p in products if p.enabled]
    logging.info(f"Checking {len(enabled)} enabled products from {args.products_csv}")

    updated = 0
    for p in enabled:
        price = extractor.extract_price(p.url, p.selector)
        if price is None:
            logging.warning(f"{p.name}: price not found")  
            continue

        now = datetime.now().isoformat()
        key = p.url  # Use URL as stable key
        rec = state.get(key, {})
        old_price = rec.get("current_price")

        rec.update({
            "name": p.name,
            "url": p.url,
            "selector": p.selector,
            "current_price": price,
            "last_checked": now,
            "last_price": old_price,
        })

        # Simple logging for price changes
        if old_price is None:
            logging.info(f"{p.name}: ${price:.2f}")
        elif price != old_price:
            logging.info(f"{p.name}: ${price:.2f} (was ${old_price:.2f})")
        else:
            logging.info(f"{p.name}: ${price:.2f} (no change)")

        state[key] = rec
        updated += 1

    save_state(args.state_file, state)
    logging.info(f"Updated {updated} products. State saved to {args.state_file}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())