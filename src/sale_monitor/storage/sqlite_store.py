from typing import List, Dict, Any
import sqlite3
import os
import logging

class SQLiteStore:
    """SQLite storage backend for storing product data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.connection = self._create_connection()
        self._create_table()

    def _create_connection(self) -> sqlite3.Connection:
        """Create a database connection to the SQLite database."""
        if not os.path.exists(self.db_path):
            logging.info(f"Database not found. Creating new database at {self.db_path}.")
        return sqlite3.connect(self.db_path)

    def _create_table(self):
        """Create the products table if it doesn't exist."""
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    target_price REAL,
                    current_price REAL,
                    discount_threshold REAL NOT NULL,
                    selector TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    last_checked TEXT,
                    last_price REAL,
                    last_notification_sent TEXT,
                    last_notification_price REAL,
                    notification_cooldown_hours INTEGER DEFAULT 24
                )
            """)

    def save_product(self, product: Dict[str, Any]):
        """Save a product to the database."""
        with self.connection:
            self.connection.execute("""
                INSERT INTO products (name, url, target_price, current_price, discount_threshold, selector, enabled, last_checked, last_price, last_notification_sent, last_notification_price, notification_cooldown_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                product['name'],
                product['url'],
                product['target_price'],
                product['current_price'],
                product['discount_threshold'],
                product['selector'],
                product['enabled'],
                product['last_checked'],
                product['last_price'],
                product['last_notification_sent'],
                product['last_notification_price'],
                product['notification_cooldown_hours']
            ))

    def load_products(self) -> List[Dict[str, Any]]:
        """Load all products from the database."""
        cursor = self.connection.cursor()
        cursor.execute("SELECT * FROM products")
        rows = cursor.fetchall()
        return [self._row_to_product_dict(row) for row in rows]

    def _row_to_product_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row to a product dictionary."""
        return {
            'id': row[0],
            'name': row[1],
            'url': row[2],
            'target_price': row[3],
            'current_price': row[4],
            'discount_threshold': row[5],
            'selector': row[6],
            'enabled': row[7],
            'last_checked': row[8],
            'last_price': row[9],
            'last_notification_sent': row[10],
            'last_notification_price': row[11],
            'notification_cooldown_hours': row[12]
        }

    def close(self):
        """Close the database connection."""
        if self.connection:
            self.connection.close()