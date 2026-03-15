"""Edge case tests for CSV parsing, SQLite resilience, and network timeouts."""
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

from sale_monitor.storage.csv_products import read_products


# ── Malformed CSV ────────────────────────────────────────────────────────────


def test_csv_missing_file():
    with pytest.raises(FileNotFoundError):
        read_products("/nonexistent/products.csv")


def test_csv_missing_required_columns(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        read_products(str(csv))


def test_csv_empty_file(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("name,url\n", encoding="utf-8")
    products = read_products(str(csv))
    assert products == []


def test_csv_extra_columns_ignored(tmp_path):
    csv = tmp_path / "extra.csv"
    csv.write_text(
        "name,url,target_price,extra_col\nWidget,https://example.com/w,,bonus\n",
        encoding="utf-8",
    )
    products = read_products(str(csv))
    assert len(products) == 1
    assert products[0].name == "Widget"


def test_csv_unicode_names(tmp_path):
    csv = tmp_path / "unicode.csv"
    csv.write_text(
        "name,url\nGadget épée 日本語,https://example.com/u\n",
        encoding="utf-8",
    )
    products = read_products(str(csv))
    assert products[0].name == "Gadget épée 日本語"


# ── Corrupted / Locked SQLite ────────────────────────────────────────────────


def test_truncated_sqlite_file(tmp_path):
    from sale_monitor.storage.price_history import PriceHistory

    db_path = tmp_path / "broken.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)

    history = PriceHistory(str(db_path))
    # Should handle gracefully — either recreate or raise a clear error
    try:
        history.record_price("https://x.com", "Test", 9.99)
    except sqlite3.DatabaseError:
        pass  # acceptable — clear error, not a hang


def test_missing_tables_creates_them(tmp_path):
    from sale_monitor.storage.price_history import PriceHistory

    db_path = tmp_path / "fresh.db"
    history = PriceHistory(str(db_path))
    # Should create tables on init; record shouldn't crash
    history.record_price("https://example.com/x", "Test", 5.99)
    records = history.get_history("https://example.com/x")
    assert len(records) >= 1


# ── Network Timeouts (Price Extractor) ───────────────────────────────────────


def test_extractor_timeout(mocker):
    from sale_monitor.services.price_extractor import PriceExtractor

    mocker.patch("time.sleep")  # no real waiting
    extractor = PriceExtractor(user_agent="test", timeout=1, max_retries=1)

    mocker.patch.object(
        extractor.session,
        "get",
        side_effect=requests.exceptions.Timeout("Connection timed out"),
    )

    price, source = extractor.extract_price("https://example.com/slow")
    assert price is None


def test_extractor_connection_refused(mocker):
    from sale_monitor.services.price_extractor import PriceExtractor

    mocker.patch("time.sleep")
    extractor = PriceExtractor(user_agent="test", timeout=1, max_retries=1)

    mocker.patch.object(
        extractor.session,
        "get",
        side_effect=requests.exceptions.ConnectionError("Connection refused"),
    )

    price, source = extractor.extract_price("https://example.com/down")
    assert price is None


def test_extractor_partial_response(mocker):
    from sale_monitor.services.price_extractor import PriceExtractor

    mocker.patch("time.sleep")
    extractor = PriceExtractor(user_agent="test", timeout=1, max_retries=1)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"<html><body>Incomplete..."
    mock_response.text = "<html><body>Incomplete..."
    mock_response.raise_for_status = lambda: None

    mocker.patch.object(extractor.session, "get", return_value=mock_response)

    price, source = extractor.extract_price("https://example.com/partial")
    assert price is None  # no price found in incomplete HTML
