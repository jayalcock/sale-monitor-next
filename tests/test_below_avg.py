"""Tests for the below_avg alert rule.

Regression: failed scrape records (price=0, status='failed') previously polluted
the 30-day average and silently suppressed notifications. The fix filters them
out so a stretch of scrape failures cannot mute legitimate price-drop alerts.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from sale_monitor.cli.main import main
from sale_monitor.storage.price_history import PriceHistory


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_EMAIL_NOTIFICATIONS", "true")
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "test@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("RECIPIENT_EMAIL", "recipient@example.com")
    monkeypatch.setenv("CHECK_INTERVAL", "")
    csv_file = tmp_path / "products.csv"
    state_file = tmp_path / "state.json"
    history_db = tmp_path / "history.db"
    return {
        "csv": str(csv_file),
        "state": str(state_file),
        "db": str(history_db),
    }


def _write_csv(path, url):
    Path(path).write_text(
        "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours,alert_rules\n"
        f"Move 2,{url},,,,true,24,below_avg\n",
        encoding="utf-8",
    )


def _seed_history(db_path: str, url: str, success_prices: list, failed_count: int):
    """Insert <success_prices> successful rows and <failed_count> failed rows."""
    history = PriceHistory(db_path)
    now = datetime.now(timezone.utc)
    for i, p in enumerate(success_prices):
        history.record_price(url, "Move 2", p, status="success", currency="CAD",
                             timestamp=(now - timedelta(days=i + 1)).isoformat())
    for i in range(failed_count):
        history.record_price(url, "Move 2", None, status="failed", currency="CAD",
                             timestamp=(now - timedelta(days=(i + 1) * 0.01)).isoformat())


def test_below_avg_ignores_failed_records(env, mocker):
    """A long stretch of failed scrapes must not drag the average to zero.

    Scenario: 30 days of failed checks (price=0) plus 5 successful records around
    $649. New price $519 should fire 'below_avg' against the success-only average
    of ~$649, not be suppressed by a polluted average near zero.
    """
    url = "https://www.sonos.com/en-ca/shop/move-2-black"
    _write_csv(env["csv"], url)
    _seed_history(env["db"], url,
                  success_prices=[649.0, 649.0, 649.0, 649.0, 649.0],
                  failed_count=200)

    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price_with_currency.return_value = (519.0, "auto", "CAD")
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification

    with patch("sys.argv", ["cli", "--products-csv", env["csv"],
                            "--state-file", env["state"],
                            "--history-db", env["db"]]):
        result = main()

    assert result == 0
    mock_send.assert_called_once()
    args, kwargs = mock_send.call_args
    assert kwargs["current_price"] == 519.0
    assert kwargs["triggered_by"] == "below_avg"


def test_below_avg_no_trigger_when_price_at_average(env, mocker):
    """Sanity: when current price matches the success-only average, no trigger."""
    url = "https://example.com/widget"
    _write_csv(env["csv"], url)
    _seed_history(env["db"], url,
                  success_prices=[100.0, 100.0, 100.0],
                  failed_count=50)

    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price_with_currency.return_value = (100.0, "auto", "CAD")
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification

    with patch("sys.argv", ["cli", "--products-csv", env["csv"],
                            "--state-file", env["state"],
                            "--history-db", env["db"]]):
        main()

    mock_send.assert_not_called()


def test_below_avg_no_history_no_trigger(env, mocker):
    """If there's no successful history at all, nothing to average against — no trigger."""
    url = "https://example.com/widget"
    _write_csv(env["csv"], url)
    _seed_history(env["db"], url, success_prices=[], failed_count=100)

    mock_extractor = mocker.patch("sale_monitor.cli.main.PriceExtractor")
    mock_extractor.return_value.extract_price_with_currency.return_value = (50.0, "auto", "CAD")
    mock_notifier = mocker.patch("sale_monitor.cli.main.NotificationManager")
    mock_send = mock_notifier.return_value.send_sale_notification

    with patch("sys.argv", ["cli", "--products-csv", env["csv"],
                            "--state-file", env["state"],
                            "--history-db", env["db"]]):
        main()

    mock_send.assert_not_called()
