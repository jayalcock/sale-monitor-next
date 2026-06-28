"""End-to-end tests for the scrape_url override:
the ProductStore must persist it, the CSV importer must read/write it,
and the model must default to None.
"""
import sqlite3

import pytest

from sale_monitor.domain.models import Product
from sale_monitor.storage.csv_products import read_products, export_products_csv
from sale_monitor.storage.migrations import run_migrations
from sale_monitor.storage.product_store import ProductStore


def _bare_history_db(path):
    """Minimum schema needed before run_migrations() can apply v1+."""
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


class TestProductModel:
    def test_scrape_url_defaults_none(self):
        p = Product(name="W", url="https://example.com/w", selector="")
        assert p.scrape_url is None

    def test_scrape_url_can_be_set(self):
        p = Product(name="W", url="https://example.com/w", selector="",
                    scrape_url="https://example.com/api/w.json")
        assert p.scrape_url == "https://example.com/api/w.json"


class TestProductStoreScrapeUrl:
    def _store(self, tmp_path):
        db = tmp_path / "h.db"
        _bare_history_db(db)
        run_migrations(str(db))
        return ProductStore(str(db))

    def test_persists_scrape_url_through_add_and_get(self, tmp_path):
        store = self._store(tmp_path)
        store.add(Product(
            name="Sonos Move 2",
            url="https://www.sonos.com/en-ca/shop/move-2-black",
            selector="",
            scrape_url="https://www.sonos.com/on/demandware.store/Sites-Sonos_CA-Site/en_CA/Product-Variation?pid=move-2-black",
        ))
        p = store.get_by_url("https://www.sonos.com/en-ca/shop/move-2-black")
        assert p is not None
        assert p.scrape_url == "https://www.sonos.com/on/demandware.store/Sites-Sonos_CA-Site/en_CA/Product-Variation?pid=move-2-black"
        # Display URL untouched
        assert p.url == "https://www.sonos.com/en-ca/shop/move-2-black"

    def test_null_scrape_url_returns_none_not_empty(self, tmp_path):
        store = self._store(tmp_path)
        store.add(Product(name="Plain", url="https://example.com/p", selector=""))
        p = store.get_by_url("https://example.com/p")
        assert p.scrape_url is None

    def test_update_can_set_and_clear_scrape_url(self, tmp_path):
        store = self._store(tmp_path)
        store.add(Product(name="W", url="https://example.com/w", selector=""))
        store.update("https://example.com/w", scrape_url="https://example.com/api/w")
        assert store.get_by_url("https://example.com/w").scrape_url == "https://example.com/api/w"
        store.update("https://example.com/w", scrape_url=None)
        assert store.get_by_url("https://example.com/w").scrape_url is None

    def test_existing_db_without_column_is_migrated(self, tmp_path):
        """A products table missing scrape_url (legacy v5 schema) must be ALTERed
        when ProductStore initializes."""
        db = tmp_path / "h.db"
        _bare_history_db(db)
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE products (
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

        store = ProductStore(str(db))  # _ensure_table must ALTER
        with sqlite3.connect(str(db)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        assert "scrape_url" in cols
        store.add(Product(name="W", url="https://example.com/w", selector="", scrape_url="https://x/y"))
        assert store.get_by_url("https://example.com/w").scrape_url == "https://x/y"


class TestCsvScrapeUrl:
    def test_read_scrape_url_column(self, tmp_path):
        csv = tmp_path / "p.csv"
        csv.write_text(
            "name,url,scrape_url\n"
            "Move 2,https://www.sonos.com/en-ca/shop/move-2-black,https://www.sonos.com/api/move-2-black\n",
            encoding="utf-8",
        )
        products = read_products(str(csv))
        assert len(products) == 1
        assert products[0].scrape_url == "https://www.sonos.com/api/move-2-black"

    def test_missing_scrape_url_column_yields_none(self, tmp_path):
        csv = tmp_path / "p.csv"
        csv.write_text("name,url\nW,https://example.com/w\n", encoding="utf-8")
        products = read_products(str(csv))
        assert products[0].scrape_url is None

    def test_empty_scrape_url_value_yields_none(self, tmp_path):
        csv = tmp_path / "p.csv"
        csv.write_text("name,url,scrape_url\nW,https://example.com/w,\n", encoding="utf-8")
        products = read_products(str(csv))
        assert products[0].scrape_url is None

    def test_roundtrip_export_then_read(self, tmp_path):
        out = tmp_path / "out.csv"
        export_products_csv(str(out), [
            Product(name="W", url="https://example.com/w", selector="",
                    scrape_url="https://api.example.com/w"),
            Product(name="X", url="https://example.com/x", selector=""),
        ])
        products = read_products(str(out))
        assert products[0].scrape_url == "https://api.example.com/w"
        assert products[1].scrape_url is None
