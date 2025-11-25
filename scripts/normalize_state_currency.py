#!/usr/bin/env python3
"""One-off normalization script to recompute currency + CAD conversions for all products.

Usage:
    PYTHONPATH=src python scripts/normalize_state_currency.py \
        --products data/products.csv \
        --state data/state.json \
        --history data/history.db

What it does:
 1. Loads products.csv
 2. For each product, fetches page once (respecting extractor settings) and extracts price + currency (HTML detection)
 3. Converts to CAD if needed (using ExchangeRateService + cached rates/price_history)
 4. Updates state.json entries: current_price, current_price_cad, currency, last_checked (preserving last_price if present)
 5. Optionally records the refreshed price into history (default ON, can disable with --no-history)

Safe to re-run; it only rewrites state for products with successfully extracted prices.
"""
import argparse
import sys
from datetime import datetime, timezone
from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state, save_state
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.services.exchange_rates import ExchangeRateService


def normalize(products_csv: str, state_file: str, history_db: str, user_agent: str, timeout: int, retries: int, record_history: bool = True):
    products = read_products(products_csv)
    state = load_state(state_file)
    history = PriceHistory(history_db)
    extractor = PriceExtractor(user_agent=user_agent, timeout=timeout, max_retries=retries)
    ex_service = ExchangeRateService(cache_handler=history)

    updated = 0
    failed = 0

    for p in products:
        price, selector_source, currency = extractor.extract_price_with_currency(p.url, p.selector, default_currency=p.currency)
        if price is None:
            failed += 1
            continue
        if currency is None:
            currency = p.currency or 'CAD'
        price_cad = price if currency == 'CAD' else ex_service.convert(float(price), currency, 'CAD')
        prev_entry = state.get(p.url, {})
        state[p.url] = {
            'current_price': price,
            'current_price_cad': price_cad,
            'currency': currency,
            'last_checked': datetime.now(timezone.utc).isoformat(),
            'last_price': prev_entry.get('current_price', price),
            'selector_source': selector_source or prev_entry.get('selector_source')
        }
        if record_history:
            history.record_price(p.url, p.name, price, status='success', currency=currency, price_cad=price_cad)
        updated += 1

    save_state(state_file, state)
    return updated, failed


def main():
    parser = argparse.ArgumentParser(description='Normalize currency and CAD values in state.json for all products.')
    parser.add_argument('--products', default='data/products.csv')
    parser.add_argument('--state', default='data/state.json')
    parser.add_argument('--history', default='data/history.db')
    parser.add_argument('--user-agent', default='Mozilla/5.0 (compatible; SaleMonitor/1.0)')
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--no-history', action='store_true', help='Do not append refreshed prices to history table.')

    args = parser.parse_args()

    print('Normalizing state...')
    try:
        updated, failed = normalize(
            args.products, args.state, args.history,
            args.user_agent, args.timeout, args.retries,
            record_history=not args.no_history
        )
        print(f'Updated {updated} products; {failed} failed.')
        if failed:
            print('Failures retained old state entries if any.')
        print('Done.')
        sys.exit(0)
    except Exception as e:  # broad for CLI feedback
        print(f'Error: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
