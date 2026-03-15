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


MIGRATIONS: List[Migration] = [
    (1, "composite index on product_url+timestamp", _migration_1_add_last_checked_index),
    (2, "index on check_status", _migration_2_add_status_index),
    (3, "create products table", _migration_3_create_products_table),
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
