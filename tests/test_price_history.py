"""
Tests for price history storage.
"""
from datetime import datetime, timedelta
from sale_monitor.storage.price_history import PriceHistory


def test_record_and_retrieve_price(tmp_path):
    """Test basic price recording and retrieval."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    # Record a price
    history.record_price(
        "https://example.com/product",
        "Test Product",
        99.99
    )
    
    # Retrieve it
    records = history.get_history("https://example.com/product")
    assert len(records) == 1
    timestamp, price, status = records[0]
    assert price == 99.99
    assert status == "success"


def test_get_all_products(tmp_path):
    """Test listing all products with history."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    history.record_price("https://example.com/a", "Product A", 10.0)
    history.record_price("https://example.com/b", "Product B", 20.0)
    history.record_price("https://example.com/a", "Product A", 12.0)
    
    products = history.get_all_products()
    assert len(products) == 2
    urls = [url for url, name in products]
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls


def test_get_price_changes(tmp_path):
    """Test detecting price changes."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    url = "https://example.com/product"
    base_time = datetime.now()
    
    # Record price changes
    history.record_price(url, "Product", 100.0, (base_time - timedelta(days=3)).isoformat())
    history.record_price(url, "Product", 100.0, (base_time - timedelta(days=2)).isoformat())
    history.record_price(url, "Product", 90.0, (base_time - timedelta(days=1)).isoformat())
    history.record_price(url, "Product", 85.0, base_time.isoformat())
    
    changes = history.get_price_changes(url)
    
    # Should have 2 changes: 100->90 and 90->85
    assert len(changes) == 2
    assert changes[0][1] == 90.0  # old price
    assert changes[0][2] == 85.0  # new price
    assert changes[1][1] == 100.0
    assert changes[1][2] == 90.0


def test_cleanup_old_records(tmp_path):
    """Test cleaning up old history records."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    url = "https://example.com/product"
    now = datetime.now()
    
    # Record old and recent prices
    history.record_price(url, "Product", 100.0, (now - timedelta(days=100)).isoformat())
    history.record_price(url, "Product", 95.0, (now - timedelta(days=50)).isoformat())
    history.record_price(url, "Product", 90.0, now.isoformat())
    
    # Keep last 60 days
    deleted = history.cleanup_old_records(60)
    assert deleted == 1
    
    # Should have 2 records left
    records = history.get_history(url)
    assert len(records) == 2


def test_get_stats(tmp_path):
    """Test price statistics calculation."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    url = "https://example.com/product"
    
    # Record various prices
    history.record_price(url, "Product", 100.0)
    history.record_price(url, "Product", 80.0)
    history.record_price(url, "Product", 120.0)
    
    stats = history.get_stats(url)
    
    assert stats["min_price"] == 80.0
    assert stats["max_price"] == 120.0
    assert stats["avg_price"] == 100.0
    assert stats["checks_count"] == 3
    assert "current_price" in stats
    assert "first_check" in stats
    assert "last_check" in stats


def test_history_with_days_filter(tmp_path):
    """Test filtering history by number of days."""
    db_path = tmp_path / "test_history.db"
    history = PriceHistory(str(db_path))
    
    url = "https://example.com/product"
    now = datetime.now()
    
    # Record prices at different times
    history.record_price(url, "Product", 100.0, (now - timedelta(days=10)).isoformat())
    history.record_price(url, "Product", 95.0, (now - timedelta(days=5)).isoformat())
    history.record_price(url, "Product", 90.0, now.isoformat())
    
    # Get last 7 days only
    records = history.get_history(url, days=7)
    assert len(records) == 2  # Should exclude the 10-day-old record


def test_export_to_csv(tmp_path):
    """Test CSV export functionality."""
    db_path = tmp_path / "test_history.db"
    csv_path = tmp_path / "export.csv"
    history = PriceHistory(str(db_path))
    
    # Record some prices
    history.record_price("https://example.com/a", "Product A", 10.0)
    history.record_price("https://example.com/b", "Product B", 20.0)
    
    # Export
    history.export_to_csv(str(csv_path))
    
    # Verify file exists and has content
    assert csv_path.exists()
    content = csv_path.read_text()
    assert "Product A" in content
    assert "Product B" in content
    assert "10.0" in content
    assert "20.0" in content
