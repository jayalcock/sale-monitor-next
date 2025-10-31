"""
SQLite-based storage for historical price data.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple


class PriceHistory:
    """Manages historical price data in SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_url TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    check_status TEXT DEFAULT 'success'
                )
            """)
            # Index for faster queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_product_url 
                ON price_history(product_url)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp 
                ON price_history(timestamp)
            """)
            conn.commit()

    def record_price(
        self, 
        product_url: str, 
        product_name: str, 
        price: float, 
        timestamp: Optional[str] = None,
        status: str = "success"
    ):
        """Record a price check."""
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO price_history (product_url, product_name, price, timestamp, check_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (product_url, product_name, price, timestamp, status)
            )
            conn.commit()

    def get_history(
        self, 
        product_url: str, 
        days: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Tuple[str, float, str]]:
        """
        Get price history for a product.
        
        Returns list of (timestamp, price, status) tuples.
        """
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT timestamp, price, check_status 
                FROM price_history 
                WHERE product_url = ?
            """
            params = [product_url]
            
            if days is not None:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                query += " AND timestamp >= ?"
                params.append(cutoff)
            
            query += " ORDER BY timestamp DESC"
            
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def get_all_products(self) -> List[Tuple[str, str]]:
        """Get list of all products with history. Returns (url, name) tuples."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT DISTINCT product_url, product_name 
                FROM price_history 
                ORDER BY product_name
            """)
            return cursor.fetchall()

    def get_price_changes(
        self, 
        product_url: str, 
        days: Optional[int] = None
    ) -> List[Tuple[str, float, float]]:
        """
        Get only records where price changed.
        
        Returns list of (timestamp, old_price, new_price) tuples.
        """
        history = self.get_history(product_url, days=days)
        if not history:
            return []
        
        changes = []
        prev_price = None
        
        for timestamp, price, status in reversed(history):  # oldest to newest
            if status != "success":
                continue
            if prev_price is not None and price != prev_price:
                changes.append((timestamp, prev_price, price))
            prev_price = price
        
        return list(reversed(changes))  # newest to oldest

    def cleanup_old_records(self, retention_days: int):
        """Delete records older than retention_days."""
        if retention_days <= 0:
            return  # 0 means keep forever
        
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "DELETE FROM price_history WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = result.rowcount
            conn.commit()
            return deleted

    def get_stats(self, product_url: str, days: Optional[int] = None) -> dict:
        """Get statistics for a product."""
        history = self.get_history(product_url, days=days)
        if not history:
            return {}
        
        prices = [price for _, price, status in history if status == "success"]
        if not prices:
            return {}
        
        return {
            "min_price": min(prices),
            "max_price": max(prices),
            "avg_price": sum(prices) / len(prices),
            "current_price": prices[0],  # Most recent
            "checks_count": len(prices),
            "first_check": history[-1][0],
            "last_check": history[0][0],
        }

    def export_to_csv(self, output_path: str, product_url: Optional[str] = None):
        """Export history to CSV file."""
        import csv
        
        with sqlite3.connect(self.db_path) as conn:
            if product_url:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, timestamp, check_status 
                    FROM price_history 
                    WHERE product_url = ?
                    ORDER BY timestamp DESC
                    """,
                    (product_url,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, timestamp, check_status 
                    FROM price_history 
                    ORDER BY timestamp DESC
                    """
                )
            
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['product_name', 'product_url', 'price', 'timestamp', 'status'])
                writer.writerows(cursor)

    def export_to_csv_stream(self, output_stream, product_url: Optional[str] = None):
        """Export history to a CSV stream (file-like object)."""
        import csv
        
        with sqlite3.connect(self.db_path) as conn:
            if product_url:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, timestamp, check_status 
                    FROM price_history 
                    WHERE product_url = ?
                    ORDER BY timestamp DESC
                    """,
                    (product_url,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, timestamp, check_status 
                    FROM price_history 
                    ORDER BY timestamp DESC
                    """
                )
            
            writer = csv.writer(output_stream)
            writer.writerow(['product_name', 'product_url', 'price', 'timestamp', 'status'])
            writer.writerows(cursor)
