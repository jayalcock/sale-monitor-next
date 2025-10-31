#!/usr/bin/env python3
"""
Sale Monitor CLI - Command-line interface for the Sale Monitor application.
"""
import argparse
import logging
import os
import time
from datetime import datetime, timedelta

import schedule
from dotenv import load_dotenv

from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state, save_state
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.services.notifications import NotificationManager, SmtpConfig


def _str_to_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def check_prices(args, smtp_cfg, notifier, extractor, history=None):
    """Check prices for all products - extracted for scheduling."""
    products = read_products(args.products_csv)
    state = load_state(args.state_file)

    enabled = [p for p in products if p.enabled]
    logging.info(f"Checking {len(enabled)} enabled products from {args.products_csv}")

    updated = 0
    for p in enabled:
        price, selector_source = extractor.extract_price(p.url, p.selector)
        if price is None:
            logging.warning(f"{p.name}: price not found")
            continue
        
        # Record price in history
        if history:
            history.record_price(p.url, p.name, price)

        now = datetime.now().isoformat()
        key = p.url  # Use URL as stable key
        rec = state.get(key, {})
        old_price = rec.get("current_price")

        # Persist price check
        rec.update({
            "name": p.name,
            "url": p.url,
            "selector": p.selector,
            "selector_source": selector_source,  # Track how selector was determined
            "current_price": price,
            "last_checked": now,
            "last_price": old_price,
        })

        # Log price change
        if old_price is None:
            logging.info(f"{p.name}: ${price:.2f}")
        elif price != old_price:
            logging.info(f"{p.name}: ${price:.2f} (was ${old_price:.2f})")
        else:
            logging.info(f"{p.name}: ${price:.2f} (no change)")

        # Determine if we should notify
        should_notify = False
        triggered_by = None

        # Target price trigger
        if p.target_price is not None and price <= p.target_price:
            should_notify = True
            triggered_by = "target_price"

        # Discount threshold trigger (requires a prior price)
        if not should_notify and p.discount_threshold is not None and old_price is not None:
            try:
                threshold_price = float(old_price) * (1 - float(p.discount_threshold) / 100.0)
                if price <= threshold_price:
                    should_notify = True
                    triggered_by = f"discount_{p.discount_threshold:.0f}%"
            except Exception:
                pass

        # Cooldown and de-dup checks
        if should_notify and smtp_cfg.enable:
            cooldown_hours = p.notification_cooldown_hours or args.default_cooldown_hours
            last_sent_str = rec.get("last_notification_sent")
            last_sent = None
            if last_sent_str:
                try:
                    last_sent = datetime.fromisoformat(last_sent_str)
                except Exception:
                    last_sent = None

            in_cooldown = False
            if last_sent:
                in_cooldown = datetime.now() < (last_sent + timedelta(hours=cooldown_hours))

            last_notified_price = rec.get("last_notification_price")

            # Suppress only if within cooldown AND price hasn't changed
            if in_cooldown and last_notified_price is not None and float(last_notified_price) == float(price):
                # Within cooldown and same price as last notification -> skip
                logging.info(f"{p.name}: notification suppressed (cooldown, same price)")
                pass
            else:
                # Send email
                try:
                    notifier.send_sale_notification(
                        product_name=p.name,
                        product_url=p.url,
                        current_price=price,
                        old_price=old_price,
                        target_price=p.target_price,
                        triggered_by=triggered_by or "rule",
                    )
                    rec["last_notification_sent"] = datetime.now().isoformat()
                    rec["last_notification_price"] = price
                    logging.info(f"{p.name}: notification sent")
                except Exception as e:
                    logging.error(f"{p.name}: email failed: {e}")

        state[key] = rec
        updated += 1

    save_state(args.state_file, state)
    logging.info(f"Updated {updated} products. State saved to {args.state_file}.")
    return updated


def main() -> int:
    load_dotenv(override=False)

    parser = argparse.ArgumentParser(description="Sale Monitor - CSV-backed")
    parser.add_argument("--products-csv", default=os.getenv("PRODUCTS_CSV", "data/products.csv"))
    parser.add_argument("--state-file", default=os.getenv("STATE_FILE", "data/state.json"))
    parser.add_argument("--history-db", default=os.getenv("HISTORY_DB", "data/history.db"))
    parser.add_argument("--history-retention-days", type=int, default=int(os.getenv("HISTORY_RETENTION_DAYS", "90")))
    parser.add_argument("--user-agent", default=os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; SaleMonitor/1.0)"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT", "30")))
    parser.add_argument("--max-retries", type=int, default=int(os.getenv("MAX_RETRIES", "3")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--default-cooldown-hours", type=int, default=int(os.getenv("NOTIFICATION_COOLDOWN_HOURS", "24")))
    parser.add_argument("--every", default=os.getenv("CHECK_INTERVAL", ""), 
                       help="Run continuously at interval (e.g., '15m', '1h', '30s'). Omit for one-time run.")
    
    # Query commands
    parser.add_argument("--show-history", metavar="PRODUCT_NAME",
                       help="Show price history for a product")
    parser.add_argument("--show-stats", metavar="PRODUCT_NAME",
                       help="Show price statistics for a product")
    parser.add_argument("--list-products", action="store_true",
                       help="List all products with history")
    parser.add_argument("--export-csv", metavar="OUTPUT_FILE",
                       help="Export history to CSV file")
    parser.add_argument("--days", type=int, default=30,
                       help="Number of days for history queries (default: 30)")
    
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s - %(levelname)s - %(message)s")

    # Initialize history
    history = PriceHistory(args.history_db)
    
    # Handle query commands
    if args.list_products:
        products = history.get_all_products()
        if not products:
            print("No products found in history.")
            return 0
        print(f"\n{'Product Name':<50} {'URL':<50}")
        print("=" * 100)
        for url, name in products:
            print(f"{name:<50} {url:<50}")
        return 0
    
    if args.show_history:
        product_name = args.show_history
        products = read_products(args.products_csv)
        product = next((p for p in products if p.name.lower() == product_name.lower()), None)
        
        if not product:
            print(f"Product '{product_name}' not found in {args.products_csv}")
            return 1
        
        hist = history.get_history(product.url, days=args.days, limit=100)
        if not hist:
            print(f"No history found for '{product_name}'")
            return 0
        
        print(f"\nPrice History for: {product.name}")
        print(f"URL: {product.url}")
        print(f"Last {args.days} days (max 100 records)\n")
        print(f"{'Timestamp':<20} {'Price':<10} {'Status':<10}")
        print("=" * 40)
        for timestamp, price, status in hist:
            print(f"{timestamp:<20} ${price:<9.2f} {status:<10}")
        return 0
    
    if args.show_stats:
        product_name = args.show_stats
        products = read_products(args.products_csv)
        product = next((p for p in products if p.name.lower() == product_name.lower()), None)
        
        if not product:
            print(f"Product '{product_name}' not found in {args.products_csv}")
            return 1
        
        stats = history.get_stats(product.url, days=args.days)
        if not stats:
            print(f"No statistics available for '{product_name}'")
            return 0
        
        print(f"\nPrice Statistics for: {product.name}")
        print(f"Period: Last {args.days} days")
        print("=" * 40)
        print(f"Current Price:  ${stats['current_price']:.2f}")
        print(f"Minimum Price:  ${stats['min_price']:.2f}")
        print(f"Maximum Price:  ${stats['max_price']:.2f}")
        print(f"Average Price:  ${stats['avg_price']:.2f}")
        print(f"Checks Count:   {stats['checks_count']}")
        print(f"First Check:    {stats['first_check']}")
        print(f"Last Check:     {stats['last_check']}")
        return 0
    
    if args.export_csv:
        history.export_to_csv(args.export_csv)
        print(f"History exported to: {args.export_csv}")
        return 0

    # Email configuration
    smtp_cfg = SmtpConfig(
        server=os.getenv("SMTP_SERVER", ""),
        port=int(os.getenv("SMTP_PORT", "587")),
        username=os.getenv("SMTP_USERNAME", ""),
        password=os.getenv("SMTP_PASSWORD", ""),
        from_email=os.getenv("FROM_EMAIL", os.getenv("SMTP_USERNAME", "")),
        to_email=os.getenv("RECIPIENT_EMAIL", ""),
        enable=_str_to_bool(os.getenv("ENABLE_EMAIL_NOTIFICATIONS", "false")),
        use_starttls=_str_to_bool(os.getenv("SMTP_STARTTLS", "true"), True),
    )
    notifier = NotificationManager(smtp_cfg)
    extractor = PriceExtractor(user_agent=args.user_agent, timeout=args.timeout, max_retries=args.max_retries)

    # Cleanup old history records
    if args.history_retention_days > 0:
        deleted = history.cleanup_old_records(args.history_retention_days)
        if deleted:
            logging.info(f"Cleaned up {deleted} old history records (retention: {args.history_retention_days} days)")

    # One-time run or scheduled?
    if not args.every:
        # One-time check
        check_prices(args, smtp_cfg, notifier, extractor, history)
        return 0

    # Parse interval
    interval = args.every.strip().lower()
    if interval.endswith('m'):
        minutes = int(interval[:-1])
        schedule.every(minutes).minutes.do(lambda: check_prices(args, smtp_cfg, notifier, extractor, history))
        logging.info(f"Scheduler started: checking every {minutes} minute(s)")
    elif interval.endswith('h'):
        hours = int(interval[:-1])
        schedule.every(hours).hours.do(lambda: check_prices(args, smtp_cfg, notifier, extractor, history))
        logging.info(f"Scheduler started: checking every {hours} hour(s)")
    elif interval.endswith('s'):
        seconds = int(interval[:-1])
        schedule.every(seconds).seconds.do(lambda: check_prices(args, smtp_cfg, notifier, extractor, history))
        logging.info(f"Scheduler started: checking every {seconds} second(s)")
    else:
        logging.error(f"Invalid interval format: {interval}. Use format like '15m', '1h', '30s'")
        return 1

    # Run once immediately, then on schedule
    check_prices(args, smtp_cfg, notifier, extractor, history)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())