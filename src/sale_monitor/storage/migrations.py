"""SQLite schema versioning and migration runner for price_history.db."""
import logging
import sqlite3
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)

# Each migration is (version, description, callable(conn)).
# Migrations are applied in order.  Version numbers must be sequential starting at 1.
Migration = Tuple[int, str, Callable[[sqlite3.Connection], None]]


def _migration_1_add_last_checked_index(conn: sqlite3.Connection) -> None:
    """Add composite index for common product+timestamp lookups."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ph_url_timestamp "
        "ON price_history(product_url, timestamp)"
    )


def _migration_2_add_status_index(conn: sqlite3.Connection) -> None:
    """Index on check_status for failure-rate queries."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ph_status "
        "ON price_history(check_status)"
    )


def _migration_3_create_products_table(conn: sqlite3.Connection) -> None:
    """Create products table — source of truth for product definitions."""
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
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_url ON products(url)"
    )


def _migration_4_backfill_price_cad(conn: sqlite3.Connection) -> None:
    """Backfill NULL price_cad for non-CAD rows using cached or fallback rates.

    Uses the latest cached exchange rate when available, otherwise falls back
    to reasonable approximations.  This is a one-time migration that locks in
    an approximate base-currency price so the chart no longer converts old
    records with today's live rate (which hides real variation).
    """
    # Collect currencies that have NULL price_cad rows
    rows = conn.execute(
        "SELECT DISTINCT UPPER(currency) FROM price_history "
        "WHERE price_cad IS NULL AND currency IS NOT NULL AND UPPER(currency) <> 'CAD'"
    ).fetchall()

    for (cur,) in rows:
        # Try to get rate from the exchange_rates cache table
        rate_row = conn.execute(
            "SELECT rate FROM exchange_rates "
            "WHERE base_currency = ? AND target_currency = 'CAD' "
            "ORDER BY timestamp DESC LIMIT 1",
            (cur,),
        ).fetchone()

        if rate_row:
            rate = rate_row[0]
        else:
            # Hardcoded fallbacks (approximate March 2026 rates)
            fallback = {"USD": 1.37, "AUD": 0.90, "EUR": 1.53, "GBP": 1.78}
            rate = fallback.get(cur)
            if rate is None:
                logger.warning("No exchange rate for %s→CAD, skipping backfill", cur)
                continue

        updated = conn.execute(
            "UPDATE price_history SET price_cad = ROUND(price * ?, 2) "
            "WHERE UPPER(currency) = ? AND price_cad IS NULL",
            (rate, cur),
        ).rowcount
        logger.info("Backfilled %d %s rows with rate %s", updated, cur, rate)


def _migration_5_create_purchases_table(conn: sqlite3.Connection) -> None:
    """Create purchases table for tracking bought items and savings."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_url TEXT NOT NULL,
            product_name TEXT NOT NULL,
            purchase_price REAL NOT NULL,
            currency TEXT DEFAULT 'CAD',
            purchase_price_base REAL,
            reference_price REAL,
            reference_price_base REAL,
            savings_base REAL,
            purchased_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchases_url ON purchases(product_url)"
    )


MIGRATIONS: List[Migration] = [
    (1, "composite index on product_url+timestamp", _migration_1_add_last_checked_index),
    (2, "index on check_status", _migration_2_add_status_index),
    (3, "create products table", _migration_3_create_products_table),
    (4, "backfill NULL price_cad with approximate rates", _migration_4_backfill_price_cad),
    (5, "create purchases table", _migration_5_create_purchases_table),
]


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY"
        ")"
    )


def get_current_version(conn: sqlite3.Connection) -> int:
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def run_migrations(db_path: str) -> int:
    """Apply pending migrations. Returns number of migrations applied."""
    applied = 0
    with sqlite3.connect(db_path) as conn:
        current = get_current_version(conn)
        for version, desc, fn in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Applying migration %d: %s", version, desc)
            fn(conn)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            applied += 1
        conn.commit()
    return applied
