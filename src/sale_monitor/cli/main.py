#!/usr/bin/env python3
"""
Sale Monitor CLI - Command-line interface for the Sale Monitor application.
"""
import argparse
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import schedule
from dotenv import load_dotenv

from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.services.price_check import PriceCheckService
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.storage.config_store import load_notification_config
from sale_monitor.storage.json_state import load_state, mutate_state, prune_stale_entries
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.storage.product_store import ProductStore
from sale_monitor.services.notifications import NotificationManager, SmtpConfig
from sale_monitor.services.webhooks import build_notifiers_from_config
from sale_monitor.utils import env_bool, str_to_bool


def build_check_service(args, history=None) -> PriceCheckService:
    """Create the shared check service from CLI args (with safe defaults)."""
    user_agent = getattr(args, 'user_agent', None) or "Mozilla/5.0 (compatible; SaleMonitor/1.0)"
    timeout = getattr(args, 'timeout', None) or 30
    max_retries = getattr(args, 'max_retries', None) or 3
    return PriceCheckService(
        user_agent=user_agent,
        timeout=timeout,
        max_retries=max_retries,
        history=history,
        ex_service=ExchangeRateService(cache_handler=history),
        config_file=os.getenv('CONFIG_FILE', 'data/config.json'),
        # Fresh extractor per check; referencing the name imported here keeps
        # the class patchable in tests via sale_monitor.cli.main.PriceExtractor.
        extractor_factory=lambda: PriceExtractor(
            user_agent=user_agent, timeout=timeout, max_retries=max_retries
        ),
    )


def _cooldown_active(now_dt, last_sent_str, cooldown_hours) -> bool:
    """True if *now_dt* is still within the cooldown window after *last_sent_str*."""
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
    except (ValueError, TypeError):
        return False
    # Normalize aware/naive mismatches (legacy state entries were naive)
    if (last_sent.tzinfo is None) != (now_dt.tzinfo is None):
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        else:
            last_sent = last_sent.replace(tzinfo=None)
    return now_dt < (last_sent + timedelta(hours=cooldown_hours))


def _evaluate_alert_rules(p, price, old_price, history):
    """Return the triggered-by label for the first matching alert rule, or None."""
    rules = p.alert_rules if p.alert_rules else ['target', 'discount']

    for rule in rules:
        if rule == 'target' and p.target_price is not None and price <= p.target_price:
            return "target_price"
        if rule == 'discount' and p.discount_threshold is not None and old_price is not None:
            try:
                threshold_price = float(old_price) * (1 - float(p.discount_threshold) / 100.0)
                if price <= threshold_price:
                    return f"discount_{p.discount_threshold:.0f}%"
            except (TypeError, ValueError):
                pass
        if rule == 'any_change' and old_price is not None and price != old_price:
            return "any_change"
        if rule == 'price_drop' and old_price is not None and price < old_price:
            return "price_drop"
        if rule == 'below_avg' and history is not None:
            try:
                records = history.get_history(p.url, days=30)
                # Failed checks are stored with price 0 — only successful
                # checks may contribute to the average.
                prices_hist = [
                    r[1] for r in records
                    if r[2] == 'success' and r[1] is not None
                ]
                if prices_hist:
                    avg = sum(prices_hist) / len(prices_hist)
                    if price < avg:
                        return "below_avg"
            except Exception:
                pass
    return None


def check_prices(args, smtp_cfg, notifier, service=None, history=None, store=None):
    """Check prices for all products - extracted for scheduling."""
    if store is None:
        store = ProductStore(args.history_db)
    if service is None:
        service = build_check_service(args, history=history)
    products = store.get_all()
    # Snapshot for decision-making (old price, cooldown bookkeeping); the
    # final write re-reads the file under the lock so concurrent web writes
    # to other products aren't clobbered.
    state = load_state(args.state_file)

    # Build webhook notifiers (Discord/Slack) once per run so the monitor
    # loop dispatches to every enabled channel, not just email.
    config_file = os.getenv('CONFIG_FILE', 'data/config.json')
    notif_cfg = load_notification_config(config_file)
    webhook_notifiers = build_notifiers_from_config(notif_cfg.get('webhooks', []))
    if webhook_notifiers:
        logging.info(
            f"Webhook channels active: {', '.join(n.name for n in webhook_notifiers)}"
        )

    enabled = [p for p in products if p.enabled]
    logging.info(f"Checking {len(enabled)} enabled products")

    # Phase 1: Extract prices in parallel (I/O bound)
    results = []
    if enabled:
        max_workers = min(4, len(enabled))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_product = {pool.submit(service.check, p): p for p in enabled}
            for future in as_completed(future_to_product):
                try:
                    results.append(future.result())
                except Exception as e:
                    p = future_to_product[future]
                    logging.error(f"{p.name}: extraction thread error: {e}")

    # Keep results in product order for stable logs
    order = {p.url: i for i, p in enumerate(enabled)}
    results.sort(key=lambda r: order.get(r.product.url, 0))

    # Phase 2: Process results sequentially (notifications, state updates)
    failure_alert_after = int(os.getenv('FAILURE_ALERT_CONSECUTIVE', '3'))
    to_apply = []
    for result in results:
        p = result.product
        if not result.success:
            logging.warning(f"{p.name}: price not found")
            # Alert exactly once when a product crosses the consecutive-failure
            # threshold (selector rot, redesign, bot blocking).  A recovery
            # followed by another streak alerts again.
            if smtp_cfg.enable and history is not None and failure_alert_after > 0:
                try:
                    streak = history.get_consecutive_failures(p.url)
                    if streak == failure_alert_after:
                        notifier.send_failure_notification(p.name, p.url, streak)
                        logging.info(f"{p.name}: failure notification sent ({streak} consecutive failures)")
                except Exception as e:
                    logging.error(f"{p.name}: failure notification error: {e}")
            continue

        price = result.price
        rec = state.get(p.url, {})
        if not isinstance(rec, dict):
            rec = {}
        old_price = rec.get("current_price")

        # Log price change
        if old_price is None:
            logging.info(f"{p.name}: ${price:.2f}")
        elif price != old_price:
            logging.info(f"{p.name}: ${price:.2f} (was ${old_price:.2f})")
        else:
            logging.info(f"{p.name}: ${price:.2f} (no change)")

        triggered_by = _evaluate_alert_rules(p, price, old_price, history)

        # Cooldown and de-dup checks
        extra_fields = {}
        if triggered_by and (smtp_cfg.enable or webhook_notifiers):
            now_dt = datetime.now(timezone.utc)
            cooldown_hours = p.notification_cooldown_hours or args.default_cooldown_hours
            in_cooldown = _cooldown_active(now_dt, rec.get("last_notification_sent"), cooldown_hours)
            last_notified_price = rec.get("last_notification_price")

            # Better-price gate: within the cooldown window, only re-notify if
            # the price has dropped BELOW the price we last notified at. This
            # stops spam from small fluctuations and from persistent rules like
            # below_avg that are true on every check. A genuine new low still
            # alerts immediately, even inside the cooldown window.
            if in_cooldown and last_notified_price is not None and float(price) >= float(last_notified_price):
                logging.info(f"{p.name}: notification suppressed (cooldown, no new low)")
            else:
                # Dispatch to every configured channel: email (if enabled) and
                # all enabled webhooks. State timestamps advance only if a
                # channel succeeds, so a broken channel can't mark an alert
                # delivered, and the cooldown applies across channels.
                sent_any = False
                if smtp_cfg.enable:
                    try:
                        notifier.send_sale_notification(
                            product_name=p.name,
                            product_url=p.url,
                            current_price=price,
                            currency=result.currency,
                            price_in_base=result.price_in_base,
                            base_currency=result.base_currency,
                            old_price=old_price,
                            target_price=p.target_price,
                            triggered_by=triggered_by or "rule",
                        )
                        sent_any = True
                    except Exception as e:
                        logging.error(f"{p.name}: email failed: {e}")
                for wn in webhook_notifiers:
                    try:
                        ok = wn.send(
                            product_name=p.name,
                            product_url=p.url,
                            current_price=price,
                            currency=result.currency,
                            price_in_base=result.price_in_base,
                            base_currency=result.base_currency,
                            old_price=old_price,
                            target_price=p.target_price,
                            triggered_by=triggered_by or "rule",
                        )
                        sent_any = sent_any or bool(ok)
                    except Exception as e:
                        logging.error(f"{p.name}: webhook {wn.name} failed: {e}")
                if sent_any:
                    extra_fields["last_notification_sent"] = datetime.now(timezone.utc).isoformat()
                    extra_fields["last_notification_price"] = price
                    logging.info(f"{p.name}: notification sent")

        to_apply.append((result, extra_fields))

    # Save state once, merging against the current file contents so writes
    # from the web app during this cycle survive.
    def _mutator(fresh_state):
        for result, extra_fields in to_apply:
            service.apply_to_state(fresh_state, result, extra_fields=extra_fields)

    mutate_state(args.state_file, _mutator)
    updated = len(to_apply)
    logging.info(f"Updated {updated} products. State saved to {args.state_file}.")
    return updated


def main() -> int:
    load_dotenv(override=False)

    parser = argparse.ArgumentParser(description="Sale Monitor")
    parser.add_argument("--products-csv", default=os.getenv("PRODUCTS_CSV", "data/products.csv"),
                       help="CSV file for initial import (one-time migration to DB)")
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

    from sale_monitor.logging_config import setup_logging
    setup_logging(level=args.log_level)

    # Initialize history and product store
    history = PriceHistory(args.history_db)
    store = ProductStore(args.history_db)

    # Auto-import from CSV on first run (one-time migration)
    import pathlib
    csv_path = args.products_csv
    if pathlib.Path(csv_path).exists():
        imported = store.auto_import_csv(csv_path)
        if imported:
            logging.info(f"Imported {imported} products from {csv_path} into database")

    # Handle query commands
    if args.list_products:
        product_rows = history.get_all_products()
        if not product_rows:
            print("No products found in history.")
            return 0
        print(f"\n{'Product Name':<50} {'URL':<50}")
        print("=" * 100)
        for url, name in product_rows:
            print(f"{name:<50} {url:<50}")
        return 0

    if args.show_history:
        product_name = args.show_history
        products = store.get_all()
        product = next((p for p in products if p.name.lower() == product_name.lower()), None)

        if not product:
            print(f"Product '{product_name}' not found")
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
            price_str = f"${price:<9.2f}" if price is not None else f"{'—':<10}"
            print(f"{timestamp:<20} {price_str} {status:<10}")
        return 0

    if args.show_stats:
        product_name = args.show_stats
        products = store.get_all()
        product = next((p for p in products if p.name.lower() == product_name.lower()), None)

        if not product:
            print(f"Product '{product_name}' not found")
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
        enable=str_to_bool(os.getenv("ENABLE_EMAIL_NOTIFICATIONS", "false")),
        use_starttls=str_to_bool(os.getenv("SMTP_STARTTLS", "true"), True),
    )
    notifier = NotificationManager(smtp_cfg)
    service = build_check_service(args, history=history)

    # Cleanup old history records
    if args.history_retention_days > 0:
        deleted = history.cleanup_old_records(args.history_retention_days)
        if deleted:
            logging.info(f"Cleaned up {deleted} old history records (retention: {args.history_retention_days} days)")

    # Prune stale state entries (URLs no longer in products)
    active_urls = store.urls()
    pruned = prune_stale_entries(args.state_file, active_urls)
    if pruned:
        logging.info(f"Pruned {pruned} stale state entries")

    # One-time run or scheduled?
    if not args.every:
        # One-time check
        check_prices(args, smtp_cfg, notifier, service, history, store)
        return 0

    # Parse interval like '15m', '1h', '30s'
    interval = args.every.strip().lower()
    m = re.fullmatch(r"(\d+)([smh])", interval)
    if not m:
        logging.error(f"Invalid interval format: {interval}. Use format like '15m', '1h', '30s'")
        return 1
    amount = int(m.group(1))
    unit = {"s": "seconds", "m": "minutes", "h": "hours"}[m.group(2)]
    job = schedule.every(amount)
    getattr(job, unit).do(lambda: check_prices(args, smtp_cfg, notifier, service, history, store))
    logging.info(f"Scheduler started: checking every {amount} {unit[:-1]}(s)")

    # Image warmup runs in the monitor process (not the web app) so the
    # dashboard serves cached images without spawning background threads
    # inside the Flask factory.  Throttled cross-process by ImageService.
    warmup = None
    if env_bool('ENABLE_IMAGE_WARMUP', True):
        from sale_monitor.services.product_images import ImageService
        image_service = ImageService(
            user_agent=args.user_agent,
            timeout=args.timeout,
            data_dir=str(pathlib.Path(args.history_db).parent),
        )

        def warmup():
            try:
                image_service.warmup_once(store.get_all())
            except Exception as e:
                logging.error(f"Image warmup failed: {e}")

        warmup_interval_min = int(os.getenv('IMAGE_WARMUP_INTERVAL_MIN', '360'))
        schedule.every(max(1, warmup_interval_min)).minutes.do(warmup)

    # Run once immediately, then on schedule (warmup after the first check
    # so image prefetching never delays price monitoring at startup)
    check_prices(args, smtp_cfg, notifier, service, history, store)
    if warmup is not None:
        warmup()

    try:
        while True:
            schedule.run_pending()
            # Sleep until the next job is due (or 60s max) instead of busy-polling
            idle = schedule.idle_seconds()
            if idle is None:
                time.sleep(60)
            else:
                time.sleep(max(1, min(idle, 60)))
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
