"""Integration tests: CLI check_prices flow with mocked HTTP & real fs/db."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sale_monitor.services.price_extractor import ExtractionResult
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
    "sale_monitor.services.price_extractor.PriceExtractor.extract",
    return_value=ExtractionResult(price=59.99, selector_source="auto", currency="CAD", currency_source="html"),
)
def test_check_prices_updates_state_and_history(_mock, tmp_path):
    args, store = _setup(tmp_path, 'Widget,https://example.com/w,50,10,#p,true,24\n')

    from sale_monitor.cli.main import build_check_service, check_prices
    from sale_monitor.storage.json_state import load_state
    from sale_monitor.storage.price_history import PriceHistory

    history = PriceHistory(str(tmp_path / "history.db"))
    service = build_check_service(args, history=history)

    check_prices(args, _smtp_cfg(), MagicMock(), service, history=history, store=store)

    # State should have the product
    state = load_state(str(tmp_path / "state.json"))
    assert "https://example.com/w" in state
    assert state["https://example.com/w"]["current_price"] == 59.99

    # History should have at least one record
    rows = history.get_history("https://example.com/w")
    assert len(rows) >= 1


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract",
    return_value=ExtractionResult(price=None),
)
def test_check_prices_skips_when_price_is_none(_mock, tmp_path):
    """When extraction returns None price, the product should NOT appear in state."""
    args, store = _setup(tmp_path, 'Broken,https://example.com/b,,,,true,24\n')

    from sale_monitor.cli.main import build_check_service, check_prices
    from sale_monitor.storage.json_state import load_state

    # Pass history=None so the failed check isn't recorded
    service = build_check_service(args, history=None)
    check_prices(args, _smtp_cfg(), MagicMock(), service, history=None, store=store)

    state = load_state(str(tmp_path / "state.json"))
    assert "https://example.com/b" not in state


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract",
    return_value=ExtractionResult(price=25.00, selector_source="manual", currency="USD", currency_source="html"),
)
def test_check_prices_multiple_products(_mock, tmp_path):
    rows = (
        'A,https://a.com/1,,,#p,true,24\n'
        'B,https://b.com/2,,,#p,true,24\n'
        'C,https://c.com/3,,,#p,false,24\n'  # disabled
    )
    args, store = _setup(tmp_path, rows)

    from sale_monitor.cli.main import build_check_service, check_prices
    from sale_monitor.storage.json_state import load_state

    service = build_check_service(args, history=None)
    check_prices(args, _smtp_cfg(), MagicMock(), service, store=store)

    state = load_state(str(tmp_path / "state.json"))
    # Two enabled products should be in state
    assert "https://a.com/1" in state
    assert "https://b.com/2" in state
    # Disabled product should NOT be there
    assert "https://c.com/3" not in state


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract",
    return_value=ExtractionResult(price=42.00, selector_source="auto", currency="CAD", currency_source="html"),
)
def test_check_prices_preserves_concurrent_state_writes(_mock, tmp_path):
    """Entries written by another process mid-cycle must survive the final save."""
    args, store = _setup(tmp_path, 'Widget,https://example.com/w,,,#p,true,24\n')

    from sale_monitor.cli.main import build_check_service, check_prices
    from sale_monitor.storage.json_state import load_state, save_state

    # Simulate a web write that happens after the CLI snapshot would be taken:
    # pre-seed an unrelated entry the CLI doesn't know about.
    save_state(args.state_file, {"https://other.example/x": {"current_price": 5.0}})

    service = build_check_service(args, history=None)
    check_prices(args, _smtp_cfg(), MagicMock(), service, store=store)

    state = load_state(args.state_file)
    assert "https://example.com/w" in state
    # The unrelated entry written concurrently is still there
    assert state["https://other.example/x"]["current_price"] == 5.0
