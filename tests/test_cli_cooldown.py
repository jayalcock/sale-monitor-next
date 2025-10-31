"""
Tests for CLI notification cooldown logic.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sale_monitor.cli.main import main


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Set up temporary CSV, state, and env vars for CLI testing."""
    csv_file = tmp_path / "products.csv"
    state_file = tmp_path / "state.json"
    
    # Minimal env for CLI
    monkeypatch.setenv("ENABLE_EMAIL_NOTIFICATIONS", "true")
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "test@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("RECIPIENT_EMAIL", "recipient@example.com")
    
    return {
        "csv_file": str(csv_file),
        "state_file": str(state_file),
        "tmp_path": tmp_path,
    }


def write_csv(path: str, products: list):
    """Write products to CSV."""
    Path(path).write_text(
        "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"
        + "\n".join(products),
        encoding="utf-8",
    )


def test_first_notification_sent(temp_env, mocker):
    """Test that first notification is sent when no prior state exists."""
    # Arrange
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,24"
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price.return_value = 95.0
    
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
        # Act
        result = main()
    
    # Assert
    assert result == 0
    mock_send.assert_called_once()
    args, kwargs = mock_send.call_args
    assert kwargs["product_name"] == "Product A"
    assert kwargs["current_price"] == 95.0


def test_cooldown_suppression_same_price(temp_env, mocker):
    """Test notification suppressed when same price within cooldown window."""
    # Arrange
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,24"
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price.return_value = 95.0
    
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    base_time = datetime(2025, 10, 30, 12, 0, 0)
    
    # First run - notification sent
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    assert mock_send.call_count == 1
    mock_send.reset_mock()
    
    # Second run - 1 hour later, same price, within 24h cooldown
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(hours=1)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            result = main()
    
    # Assert - no notification sent
    assert result == 0
    mock_send.assert_not_called()


def test_cooldown_expiry_sends_notification(temp_env, mocker):
    """Test notification sent again after cooldown period expires."""
    # Arrange
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,24"
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price.return_value = 95.0
    
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    base_time = datetime(2025, 10, 30, 12, 0, 0)
    
    # First run
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    assert mock_send.call_count == 1
    mock_send.reset_mock()
    
    # Second run - 25 hours later (cooldown expired)
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(hours=25)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            result = main()
    
    # Assert - notification sent again
    assert result == 0
    mock_send.assert_called_once()


def test_price_drop_during_cooldown_sends_notification(temp_env, mocker):
    """Test notification sent when price drops further during cooldown."""
    # Arrange
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,24"
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    base_time = datetime(2025, 10, 30, 12, 0, 0)
    
    # First run - price at 95.0
    mock_extractor.return_value.extract_price.return_value = 95.0
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    assert mock_send.call_count == 1
    mock_send.reset_mock()
    
    # Second run - 1 hour later, price drops to 85.0 (within cooldown but different price)
    mock_extractor.return_value.extract_price.return_value = 85.0
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(hours=1)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            result = main()
    
    # Assert - notification sent because price changed
    assert result == 0
    mock_send.assert_called_once()
    args, kwargs = mock_send.call_args
    assert kwargs["current_price"] == 85.0


def test_per_product_cooldown_from_csv(temp_env, mocker):
    """Test per-product notification_cooldown_hours from CSV overrides default."""
    # Arrange - Product with 1-hour cooldown
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,1"
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price.return_value = 95.0
    
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    base_time = datetime(2025, 10, 30, 12, 0, 0)
    
    # First run
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    assert mock_send.call_count == 1
    mock_send.reset_mock()
    
    # Second run - 30 minutes later (within 1h cooldown)
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(minutes=30)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    mock_send.assert_not_called()
    mock_send.reset_mock()
    
    # Third run - 65 minutes later (cooldown expired)
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(minutes=65)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            result = main()
    
    # Assert - notification sent after 1h cooldown expires
    assert result == 0
    mock_send.assert_called_once()


def test_multiple_products_independent_cooldowns(temp_env, mocker):
    """Test that cooldowns are tracked independently per product URL."""
    # Arrange - Two products
    write_csv(temp_env["csv_file"], [
        "Product A,https://example.com/a,100.0,,,true,24",
        "Product B,https://example.com/b,100.0,,,true,24",
    ])
    
    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification
    
    # Both products at target price
    mock_extractor.return_value.extract_price.side_effect = [95.0, 95.0]
    
    base_time = datetime(2025, 10, 30, 12, 0, 0)
    
    # First run - both notify
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            main()
    
    assert mock_send.call_count == 2
    mock_send.reset_mock()
    
    # Second run - 1 hour later, Product A changes price, Product B same
    mock_extractor.return_value.extract_price.side_effect = [85.0, 95.0]
    with patch("sale_monitor.cli.main.datetime") as mock_dt:
        mock_dt.now.return_value = base_time + timedelta(hours=1)
        mock_dt.fromisoformat = datetime.fromisoformat
        with patch("sys.argv", ["cli", "--products-csv", temp_env["csv_file"], "--state-file", temp_env["state_file"]]):
            result = main()
    
    # Assert - only Product A notifies (price changed)
    assert result == 0
    assert mock_send.call_count == 1
    args, kwargs = mock_send.call_args
    assert kwargs["product_name"] == "Product A"
    assert kwargs["current_price"] == 85.0
