"""
SQLite-based storage for historical price data.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple


class PriceHistory:
    """Manages historical price data in SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    # --- integrity helpers ---
    def _integrity_ok(self) -> bool:
        """Return True if the SQLite file passes PRAGMA integrity_check."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    row = conn.execute("PRAGMA integrity_check").fetchone()
                    return bool(row and row[0] == "ok")
                except sqlite3.DatabaseError:
                    return False
        except sqlite3.DatabaseError:
            return False

    def _quarantine_db_files(self):
        """Rename DB and its -wal/-shm to a timestamped .corrupt-* name."""
        p = Path(self.db_path)
        if not p.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        quarantine = p.parent / f"{p.name}.corrupt-{ts}"
        try:
            p.rename(quarantine)
        except (OSError, IOError):
            return
        for suffix in ("-wal", "-shm"):
            side = Path(self.db_path + suffix)
            if side.exists():
                try:
                    side.rename(Path(str(quarantine) + suffix))
                except (OSError, IOError):
                    pass

    def _init_db(self):
        """Initialize database schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Attempt to recover from transient WAL issues before quarantine
        if not self._integrity_ok():
            try:
                with sqlite3.connect(self.db_path) as conn:
                    try:
                        # Try passive checkpoint first (doesn't block writers)
                        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
            except sqlite3.Error:
                pass
            # Re-check after checkpoint attempt
            if not self._integrity_ok():
                # Only quarantine if database still fails after checkpoint
                # This avoids losing data during container restarts with WAL files
                self._quarantine_db_files()

        with sqlite3.connect(self.db_path) as conn:
            # Pragmas to improve reliability when app has concurrent read/write
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.Error:
                # Pragmas are best-effort; proceed even if not supported
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_url TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    check_status TEXT DEFAULT 'success',
                    currency TEXT DEFAULT 'CAD',
                    price_cad REAL
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
            # Composite index for the most common query pattern
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_product_url_timestamp
                ON price_history(product_url, timestamp DESC)
            """)
            # Composite index for failure stats queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_product_url_status
                ON price_history(product_url, check_status, timestamp DESC)
            """)
            
            # Exchange rates cache table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS exchange_rates (
                    base_currency TEXT NOT NULL,
                    target_currency TEXT NOT NULL,
                    rate REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (base_currency, target_currency)
                )
            """)
            
            # Migration: add currency and price_cad columns if they don't exist
            cursor = conn.execute("PRAGMA table_info(price_history)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'currency' not in columns:
                conn.execute("ALTER TABLE price_history ADD COLUMN currency TEXT DEFAULT 'CAD'")
            if 'price_cad' not in columns:
                conn.execute("ALTER TABLE price_history ADD COLUMN price_cad REAL")
            
            # Data hygiene: normalize currency to uppercase
            try:
                conn.execute("""
                    UPDATE price_history
                    SET currency = UPPER(currency)
                    WHERE currency IS NOT NULL AND currency <> UPPER(currency)
                """)
            except sqlite3.Error:
                pass

            # Drop old redundant single-column indexes (covered by composites above)
            conn.execute("DROP INDEX IF EXISTS idx_product_url")
            conn.execute("DROP INDEX IF EXISTS idx_timestamp")
            # Drop unused covering index from prior version
            conn.execute("DROP INDEX IF EXISTS idx_status_ts_url_price")

            conn.commit()

            # Keep query planner statistics fresh
            try:
                conn.execute("ANALYZE")
            except sqlite3.Error:
                pass

        # Run schema migrations (versioned, idempotent)
        from sale_monitor.storage.migrations import run_migrations
        run_migrations(self.db_path)

    def record_price(
        self, 
        product_url: str, 
        product_name: str, 
        price: float, 
        timestamp: Optional[str] = None,
        status: str = "success",
        currency: str = "CAD",
        price_cad: Optional[float] = None
    ):
        """Record a price check with currency information."""
        if timestamp is None:
            # Store UTC with offset so clients can render correctly in local time
            timestamp = datetime.now(timezone.utc).isoformat()

        # Normalize currency code
        cur = (currency or "CAD").strip().upper()
        if len(cur) != 3 or not cur.isalpha():
            cur = "CAD"

        # Auto-store price_cad for CAD entries; for non-CAD keep the
        # provided value (which captures the exchange rate at check time).
        if cur == "CAD" and price_cad is None:
            price_cad = price
        
        def _do_insert():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO price_history 
                    (product_url, product_name, price, timestamp, check_status, currency, price_cad)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (product_url, product_name, price if price is not None else 0, timestamp, status, cur, price_cad)
                )
                conn.commit()

        try:
            _do_insert()
        except sqlite3.DatabaseError:
            # If DB is malformed, quarantine and retry once
            if not self._integrity_ok():
                self._quarantine_db_files()
                self._init_db()
                _do_insert()
            else:
                raise

    def get_history_extended(
        self,
        product_url: str,
        days: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Tuple[str, float, str, str, Optional[float]]]:
        """
        Get price history for a product with currency information.

        Returns list of (timestamp, price, status, currency, price_cad) tuples.
        """
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT timestamp, price, check_status, currency, price_cad
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

    def get_history(
        self,
        product_url: str,
        days: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Tuple[str, float, str]]:
        """
        Get price history for a product (backward-compatible format).

        Returns list of (timestamp, price, status) tuples.
        """
        extended = self.get_history_extended(product_url, days=days, limit=limit)
        return [(ts, price, status) for (ts, price, status, _cur, _cad) in extended]

    def get_all_products(self) -> List[Tuple[str, str]]:
        """Get list of all products with history. Returns (url, name) tuples."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT DISTINCT product_url, product_name 
                FROM price_history 
                ORDER BY product_name
            """)
            return cursor.fetchall()

    def get_all_history_extended(
        self,
        days: Optional[int] = None,
        url_filter: Optional[set] = None,
    ) -> dict:
        """Fetch history for all products in a single query.

        Args:
            days: Restrict to records within the last N days.
            url_filter: If provided, only include these URLs.

        Returns:
            dict mapping product_url -> list of (timestamp, price, status, currency, price_cad) tuples,
            ordered newest-first per product.
        """
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT product_url, timestamp, price, check_status, currency, price_cad
                FROM price_history
            """
            params: list = []

            if days is not None:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                query += " WHERE timestamp >= ?"
                params.append(cutoff)

            query += " ORDER BY product_url, timestamp DESC"

            cursor = conn.execute(query, params)

            result: dict = {}
            for row in cursor:
                url = row[0]
                if url_filter is not None and url not in url_filter:
                    continue
                result.setdefault(url, []).append((row[1], row[2], row[3], row[4], row[5]))

            return result

    def get_price_changes(
        self, 
        product_url: str, 
        days: Optional[int] = None
    ) -> List[Tuple[str, float, float]]:
        """
        Get only records where price changed.
        
        Returns list of (timestamp, old_price, new_price) tuples.
        """
        history = self.get_history_extended(product_url, days=days)
        if not history:
            return []
        
        changes = []
        prev_price = None
        
        for timestamp, price, status, _currency, _price_cad in reversed(history):  # oldest to newest
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

    def get_failure_stats(self, product_url: str, days: int = 7) -> dict:
        """Get failure statistics for a product over the last N days.
        
        Returns:
            dict with keys: total_checks, failed_checks, failure_rate, last_success, last_failure
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            # Single query for all failure stats
            # Counts are scoped to cutoff window; last_success/last_failure span all time
            cursor = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS total,
                    SUM(CASE WHEN timestamp >= ? AND check_status != 'success' THEN 1 ELSE 0 END) AS failed,
                    MAX(CASE WHEN check_status = 'success' THEN timestamp END) AS last_success,
                    MAX(CASE WHEN check_status != 'success' THEN timestamp END) AS last_failure
                FROM price_history
                WHERE product_url = ?
                """,
                (cutoff_iso, cutoff_iso, product_url)
            )
            row = cursor.fetchone()
            total = row[0] if row and row[0] else 0
            failed = row[1] if row and row[1] else 0
            last_success = row[2] if row else None
            last_failure = row[3] if row else None
            
        failure_rate = (failed / total * 100) if total > 0 else 0
        
        return {
            'total_checks': total,
            'failed_checks': failed,
            'failure_rate': round(failure_rate, 1),
            'last_success': last_success,
            'last_failure': last_failure
        }

    def get_failure_stats_batch(self, product_urls: list, days: int = 7) -> dict:
        """Get failure statistics for multiple products in a single query.

        Returns dict mapping product_url -> failure stats dict (same shape as
        ``get_failure_stats``).  Products with no history are omitted.
        """
        if not product_urls:
            return {}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in product_urls)
            cursor = conn.execute(
                f"""
                SELECT
                    product_url,
                    COUNT(*),
                    SUM(CASE WHEN check_status != 'success' THEN 1 ELSE 0 END),
                    MAX(CASE WHEN check_status = 'success' THEN timestamp END),
                    MAX(CASE WHEN check_status != 'success' THEN timestamp END)
                FROM price_history
                WHERE product_url IN ({placeholders})
                  AND timestamp >= ?
                GROUP BY product_url
                """,
                list(product_urls) + [cutoff],
            )

            result = {}
            for row in cursor:
                url, total, failed, last_success, last_failure = row
                total = total or 0
                failed = failed or 0
                failure_rate = (failed / total * 100) if total > 0 else 0
                result[url] = {
                    "total_checks": total,
                    "failed_checks": failed,
                    "failure_rate": round(failure_rate, 1),
                    "last_success": last_success,
                    "last_failure": last_failure,
                }
            return result

    def get_recent_success_batch(self, product_urls: list, n: int = 3) -> dict:
        """Check if the last N checks for each product were all successes.

        Returns dict mapping product_url -> True if last N checks are all
        'success', False otherwise.  Products with fewer than N checks are
        considered not-all-success.
        """
        if not product_urls:
            return {}

        result = {}
        with sqlite3.connect(self.db_path) as conn:
            for url in product_urls:
                rows = conn.execute(
                    "SELECT check_status FROM price_history "
                    "WHERE product_url = ? ORDER BY timestamp DESC LIMIT ?",
                    (url, n),
                ).fetchall()
                result[url] = (
                    len(rows) >= n and all(r[0] == "success" for r in rows)
                )
        return result

    def get_max_prices_batch(self, product_urls: list, days: Optional[int] = None) -> dict:
        """Get max successful price per product in a single query.

        Returns dict mapping product_url -> max_price (float).
        Products with no successful history are omitted.
        """
        if not product_urls:
            return {}

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in product_urls)
            query = f"""
                SELECT product_url, MAX(price)
                FROM price_history
                WHERE product_url IN ({placeholders})
                  AND check_status = 'success'
            """
            params: list = list(product_urls)

            if days is not None:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                query += " AND timestamp >= ?"
                params.append(cutoff)

            query += " GROUP BY product_url"
            return {row[0]: row[1] for row in conn.execute(query, params) if row[1] is not None}

    def get_stats(self, product_url: str, days: Optional[int] = None) -> dict:
        """Get statistics for a product, using frontend-expected key names.

        Uses SQL aggregates so we never load the full history into Python.
        """
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT MIN(price), MAX(price), AVG(price), COUNT(*),
                       MIN(timestamp), MAX(timestamp)
                FROM price_history
                WHERE product_url = ? AND check_status = 'success'
            """
            params: list = [product_url]

            if days is not None:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                query += " AND timestamp >= ?"
                params.append(cutoff)

            row = conn.execute(query, params).fetchone()
            if not row or not row[3]:
                return {}

            min_price, max_price, avg_price, total, first_check, latest_check = row

            # Current price = most recent successful price
            cur_query = """
                SELECT price FROM price_history
                WHERE product_url = ? AND check_status = 'success'
            """
            cur_params: list = [product_url]
            if days is not None:
                cur_query += " AND timestamp >= ?"
                cur_params.append(cutoff)
            cur_query += " ORDER BY timestamp DESC LIMIT 1"

            cur_row = conn.execute(cur_query, cur_params).fetchone()
            current_price = cur_row[0] if cur_row else min_price

        stats = {
            "min_price": min_price,
            "max_price": max_price,
            "avg_price": avg_price,
            "current_price": current_price,
            "total_checks": total,
            "first_check": first_check,
            "latest_check": latest_check,
        }
        # Back-compat for existing callers/tests
        stats["checks_count"] = total
        stats["last_check"] = stats["latest_check"]
        return stats

    def export_to_csv(self, output_path: str, product_url: Optional[str] = None):
        """Export history to CSV file."""
        import csv
        
        with sqlite3.connect(self.db_path) as conn:
            if product_url:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, currency, price_cad, timestamp, check_status 
                    FROM price_history 
                    WHERE product_url = ?
                    ORDER BY timestamp DESC
                    """,
                    (product_url,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, currency, price_cad, timestamp, check_status 
                    FROM price_history 
                    ORDER BY timestamp DESC
                    """
                )
            
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['product_name', 'product_url', 'price', 'currency', 'price_cad', 'timestamp', 'status'])
                writer.writerows(cursor)

    def export_to_csv_stream(self, output_stream, product_url: Optional[str] = None):
        """Export history to a CSV stream (file-like object)."""
        import csv
        
        with sqlite3.connect(self.db_path) as conn:
            if product_url:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, currency, price_cad, timestamp, check_status 
                    FROM price_history 
                    WHERE product_url = ?
                    ORDER BY timestamp DESC
                    """,
                    (product_url,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, currency, price_cad, timestamp, check_status 
                    FROM price_history 
                    ORDER BY timestamp DESC
                    """
                )
            
            writer = csv.writer(output_stream)
            writer.writerow(['product_name', 'product_url', 'price', 'currency', 'price_cad', 'timestamp', 'status'])
            writer.writerows(cursor)

    def normalize_names(self, name_by_url: dict) -> int:
        """Normalize product_name values in DB to match provided mapping.

        Args:
            name_by_url: mapping of product_url -> correct display name (from CSV)

        Returns:
            The number of rows updated.
        """
        if not name_by_url:
            return 0

        total_updated = 0
        with sqlite3.connect(self.db_path) as conn:
            for url, correct_name in name_by_url.items():
                if not correct_name:
                    continue
                cur = conn.execute(
                    """
                    UPDATE price_history
                    SET product_name = ?
                    WHERE product_url = ? AND product_name <> ?
                    """,
                    (correct_name, url, correct_name),
                )
                total_updated += cur.rowcount or 0
            conn.commit()

        return total_updated

    def cache_exchange_rate(self, base_currency: str, target_currency: str, rate: float):
        """Cache an exchange rate with current timestamp."""
        timestamp = datetime.now(timezone.utc).isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO exchange_rates 
                (base_currency, target_currency, rate, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (base_currency, target_currency, rate, timestamp)
            )
            conn.commit()
    
    def get_cached_rate(self, base_currency: str, target_currency: str, max_age_hours: int = 24) -> Optional[float]:
        """Get cached exchange rate if it's fresh enough."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT rate FROM exchange_rates
                WHERE base_currency = ? AND target_currency = ?
                AND timestamp >= ?
                """,
                (base_currency, target_currency, cutoff)
            )
            result = cursor.fetchone()
            return result[0] if result else None
