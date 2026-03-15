import argparse
from typing import Dict

from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.storage.product_store import ProductStore


def build_name_map(history_db: str) -> Dict[str, str]:
    store = ProductStore(history_db)
    products = store.get_all()
    return {p.url: p.name for p in products}


def main():
    parser = argparse.ArgumentParser(description="Normalize product names in history DB from products table")
    parser.add_argument("--history-db", default="data/history.db", help="Path to SQLite history DB")
    parser.add_argument("--apply", action="store_true", help="Apply changes (otherwise dry-run)")
    args = parser.parse_args()

    name_map = build_name_map(args.history_db)
    ph = PriceHistory(args.history_db)

    if not args.apply:
        # Dry-run: report how many rows would change
        # Count rows where name differs from mapping
        changed = 0
        import sqlite3
        with sqlite3.connect(args.history_db) as conn:
            for url, name in name_map.items():
                cur = conn.execute(
                    "SELECT COUNT(1) FROM price_history WHERE product_url = ? AND product_name <> ?",
                    (url, name),
                )
                (cnt,) = cur.fetchone()
                changed += cnt or 0
        print(f"[DRY-RUN] Rows requiring update: {changed}")
        print("Use --apply to perform the update.")
        return

    updated = ph.normalize_names(name_map)
    print(f"Updated rows: {updated}")


if __name__ == "__main__":
    main()
