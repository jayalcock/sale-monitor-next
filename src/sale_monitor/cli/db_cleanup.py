import argparse
from typing import Dict

from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.storage.csv_products import read_products


def build_name_map(products_csv: str) -> Dict[str, str]:
    products = read_products(products_csv)
    return {p.url: p.name for p in products}


def main():
    parser = argparse.ArgumentParser(description="Normalize product names in history DB from products.csv")
    parser.add_argument("--products-csv", default="data/products.csv", help="Path to products.csv")
    parser.add_argument("--history-db", default="data/history.db", help="Path to SQLite history DB")
    parser.add_argument("--apply", action="store_true", help="Apply changes (otherwise dry-run)")
    args = parser.parse_args()

    name_map = build_name_map(args.products_csv)
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
