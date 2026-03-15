"""SQLite-backed product storage.

Replaces CSV as the source of truth for product definitions.
CSV is retained only as an import/export format.
"""
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from sale_monitor.domain.models import Product


class ProductStore:
    """CRUD access to the products table in the shared history database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    target_price REAL,
                    discount_threshold REAL,
                    selector TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    notification_cooldown_hours INTEGER DEFAULT 24,
                    selector_source TEXT,
                    currency TEXT DEFAULT 'CAD',
                    "group" TEXT,
                    tags TEXT DEFAULT '',
                    alert_rules TEXT DEFAULT '',
                    notification_channels TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()

    # ── Read ──────────────────────────────────────────────────────────────

    def _row_to_product(self, row: sqlite3.Row) -> Product:
        return Product(
            name=row["name"],
            url=row["url"],
            selector=row["selector"] or "",
            target_price=row["target_price"],
            discount_threshold=row["discount_threshold"],
            enabled=bool(row["enabled"]),
            notification_cooldown_hours=row["notification_cooldown_hours"] or 24,
            selector_source=row["selector_source"],
            currency=row["currency"] or "CAD",
            group=row["group"],
            tags=_parse_list(row["tags"]),
            alert_rules=_parse_list(row["alert_rules"]),
            notification_channels=_parse_list(row["notification_channels"]),
        )

    def get_all(self) -> List[Product]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM products ORDER BY id'
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def get_enabled(self) -> List[Product]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM products WHERE enabled = 1 ORDER BY id'
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def get_by_url(self, url: str) -> Optional[Product]:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM products WHERE url = ?', (url,)
            ).fetchone()
        return self._row_to_product(row) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute('SELECT COUNT(*) FROM products').fetchone()
        return row[0]

    def urls(self) -> set:
        with self._connect() as conn:
            rows = conn.execute('SELECT url FROM products').fetchall()
        return {r[0] for r in rows}

    # ── Write ─────────────────────────────────────────────────────────────

    def add(self, product: Product) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO products
                   (name, url, target_price, discount_threshold, selector,
                    enabled, notification_cooldown_hours, selector_source,
                    currency, "group", tags, alert_rules, notification_channels,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product.name,
                    product.url,
                    product.target_price,
                    product.discount_threshold,
                    product.selector,
                    1 if product.enabled else 0,
                    product.notification_cooldown_hours,
                    product.selector_source,
                    product.currency or "CAD",
                    product.group,
                    _join_list(product.tags),
                    _join_list(product.alert_rules),
                    _join_list(product.notification_channels),
                    now,
                    now,
                ),
            )
            conn.commit()

    def update(self, url: str, **fields) -> bool:
        if not fields:
            return False
        # Map Python field names to SQL column names
        col_map = {"group": '"group"'}
        # Convert list fields to comma-separated strings
        list_fields = {"tags", "alert_rules", "notification_channels"}
        set_parts = []
        values = []
        for key, val in fields.items():
            col = col_map.get(key, key)
            if key in list_fields and isinstance(val, list):
                val = _join_list(val)
            if key == "enabled":
                val = 1 if val else 0
            set_parts.append(f"{col} = ?")
            values.append(val)
        set_parts.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(url)
        sql = f"UPDATE products SET {', '.join(set_parts)} WHERE url = ?"
        with self._connect() as conn:
            cur = conn.execute(sql, values)
            conn.commit()
        return cur.rowcount > 0

    def delete(self, url: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute('DELETE FROM products WHERE url = ?', (url,))
            conn.commit()
        return cur.rowcount > 0

    def replace_all(self, products: List[Product]) -> None:
        """Replace all products atomically (used by auto-detect-all)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute('DELETE FROM products')
            for p in products:
                conn.execute(
                    """INSERT INTO products
                       (name, url, target_price, discount_threshold, selector,
                        enabled, notification_cooldown_hours, selector_source,
                        currency, "group", tags, alert_rules, notification_channels,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.name, p.url, p.target_price, p.discount_threshold,
                        p.selector, 1 if p.enabled else 0,
                        p.notification_cooldown_hours, p.selector_source,
                        p.currency or "CAD", p.group,
                        _join_list(p.tags), _join_list(p.alert_rules),
                        _join_list(p.notification_channels), now, now,
                    ),
                )
            conn.commit()

    # ── Import / Export ───────────────────────────────────────────────────

    def auto_import_csv(self, csv_path: str) -> int:
        """Import products from CSV only if the DB products table is empty.

        Returns the number of products imported (0 if table already populated).
        """
        if self.count() > 0:
            return 0
        return self.import_from_csv(csv_path)

    def import_from_csv(self, csv_path: str) -> int:
        """Import products from a CSV file, skipping URLs that already exist."""
        from sale_monitor.storage.csv_products import read_products

        products = read_products(csv_path)
        existing_urls = self.urls()
        added = 0
        for p in products:
            if p.url in existing_urls:
                continue
            self.add(p)
            existing_urls.add(p.url)
            added += 1
        return added


# ── Helpers ───────────────────────────────────────────────────────────────

def _parse_list(value) -> List[str]:
    if not value:
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _join_list(lst) -> str:
    if not lst:
        return ""
    return ",".join(str(s).strip() for s in lst if str(s).strip())
