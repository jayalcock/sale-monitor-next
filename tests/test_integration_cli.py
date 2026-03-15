"""Integration tests: CLI check_prices flow with mocked HTTP & real fs/db."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sale_monitor.storage.product_store import ProductStore

HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def _setup(tmp_path, csv_rows):
    """Create real CSV, state, history files and return CLI args namespace + store."""
    csv_path = tmp_path / "products.csv"
    state_path = tmp_path / "state.json"
    db_path = tmp_path / "history.db"
    csv_path.write_text(HEADER + csv_rows, encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    store = ProductStore(str(db_path))
    store.auto_import_csv(str(csv_path))
    args = SimpleNamespace(
        products_csv=str(csv_path),
        state_file=str(state_path),
        history_db=str(db_path),
        default_cooldown_hours=24,
    )
    return args, store


def _smtp_cfg(enable=False):
    return SimpleNamespace(
        enable=enable,
        server="localhost",
        port=25,
        username="",
        password="",
        from_email="test@example.com",
        to_email="dest@example.com",
    )


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price_with_currency",
    return_value=(59.99, "auto", "CAD"),
)
def test_check_prices_updates_state_and_history(_mock, tmp_path):
    args, store = _setup(tmp_path, 'Widget,https://example.com/w,50,10,#p,true,24\n')

    from sale_monitor.cli.main import check_prices
    from sale_monitor.services.price_extractor import PriceExtractor
    from sale_monitor.storage.json_state import load_state
    from sale_monitor.storage.price_history import PriceHistory

    extractor = PriceExtractor.__new__(PriceExtractor)
    extractor.last_identifiers = {}
    history = PriceHistory(str(tmp_path / "history.db"))

    check_prices(args, _smtp_cfg(), MagicMock(), extractor, history=history, store=store)

    # State should have the product
    state = load_state(str(tmp_path / "state.json"))
    assert "https://example.com/w" in state
    assert state["https://example.com/w"]["current_price"] == 59.99

    # History should have at least one record
    rows = history.get_history("https://example.com/w")
    assert len(rows) >= 1


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price_with_currency",
    return_value=(None, None, None),
)
def test_check_prices_skips_when_price_is_none(_mock, tmp_path):
    """When extraction returns None price, the product should NOT appear in state."""
    args, store = _setup(tmp_path, 'Broken,https://example.com/b,,,,true,24\n')

    from sale_monitor.cli.main import check_prices
    from sale_monitor.services.price_extractor import PriceExtractor
    from sale_monitor.storage.json_state import load_state

    extractor = PriceExtractor.__new__(PriceExtractor)
    extractor.last_identifiers = {}

    # Pass history=None to avoid NOT NULL constraint on price column
    check_prices(args, _smtp_cfg(), MagicMock(), extractor, history=None, store=store)

    state = load_state(str(tmp_path / "state.json"))
    assert "https://example.com/b" not in state


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price_with_currency",
    return_value=(25.00, "manual", "USD"),
)
def test_check_prices_multiple_products(_mock, tmp_path):
    rows = (
        'A,https://a.com/1,,,#p,true,24\n'
        'B,https://b.com/2,,,#p,true,24\n'
        'C,https://c.com/3,,,#p,false,24\n'  # disabled
    )
    args, store = _setup(tmp_path, rows)

    from sale_monitor.cli.main import check_prices
    from sale_monitor.services.price_extractor import PriceExtractor
    from sale_monitor.storage.json_state import load_state

    extractor = PriceExtractor.__new__(PriceExtractor)
    extractor.last_identifiers = {}

    check_prices(args, _smtp_cfg(), MagicMock(), extractor, store=store)

    state = load_state(str(tmp_path / "state.json"))
    # Two enabled products should be in state
    assert "https://a.com/1" in state
    assert "https://b.com/2" in state
    # Disabled product should NOT be there
    assert "https://c.com/3" not in state
