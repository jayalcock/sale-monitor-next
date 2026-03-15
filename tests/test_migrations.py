"""Tests for SQLite schema versioning and migrations."""
import sqlite3

from sale_monitor.storage.migrations import get_current_version, run_migrations


def _create_bare_db(path):
    """Create a price_history DB with base schema, no migrations applied."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute("""
            CREATE TABLE price_history (
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
        conn.commit()


def test_run_migrations_on_fresh_db(tmp_path):
    db = tmp_path / "test.db"
    _create_bare_db(db)

    applied = run_migrations(str(db))
    assert applied == 3

    with sqlite3.connect(str(db)) as conn:
        version = get_current_version(conn)
    assert version == 3


def test_migrations_are_idempotent(tmp_path):
    db = tmp_path / "test.db"
    _create_bare_db(db)

    run_migrations(str(db))
    applied = run_migrations(str(db))  # second run
    assert applied == 0

    with sqlite3.connect(str(db)) as conn:
        assert get_current_version(conn) == 3


def test_indices_exist_after_migration(tmp_path):
    db = tmp_path / "test.db"
    _create_bare_db(db)
    run_migrations(str(db))

    with sqlite3.connect(str(db)) as conn:
        indices = [
            row[1]
            for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='price_history'"
            ).fetchall()
        ]
    assert "idx_ph_url_timestamp" in indices
    assert "idx_ph_status" in indices


def test_partial_migration_resumes(tmp_path):
    """If only migration 1 was applied, running again applies only migration 2."""
    db = tmp_path / "test.db"
    _create_bare_db(db)

    # Manually apply migration 1 only
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)", )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ph_url_timestamp "
            "ON price_history(product_url, timestamp)"
        )
        conn.commit()

    applied = run_migrations(str(db))
    assert applied == 2

    with sqlite3.connect(str(db)) as conn:
        assert get_current_version(conn) == 3
