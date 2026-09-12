"""Tests for the shared PriceCheckService and CLI alert-rule fixes."""
from types import SimpleNamespace

from sale_monitor.cli.main import _evaluate_alert_rules
from sale_monitor.domain.models import Product
from sale_monitor.services.price_check import CheckResult, PriceCheckService
from sale_monitor.storage.price_history import PriceHistory


def _product(**kw):
    defaults = dict(name="Widget", url="https://example.com/w", selector="#p")
    defaults.update(kw)
    return Product(**defaults)


# ── below_avg rule ─────────────────────────────────────────────────────────

class _FakeHistory:
    def __init__(self, rows):
        self._rows = rows

    def get_history(self, url, days=30):
        return self._rows


def test_below_avg_ignores_failed_checks():
    """Failed checks are stored with price 0 and must not drag the average down."""
    p = _product(alert_rules=["below_avg"])
    rows = [
        ("t1", 100.0, "success"),
        ("t2", 0.0, "failed"),
        ("t3", 0.0, "failed"),
        ("t4", 100.0, "success"),
    ]
    # Real average is 100; with the zeros it would be 50 and 90 would not alert.
    assert _evaluate_alert_rules(p, 90.0, None, _FakeHistory(rows)) == "below_avg"
    # At the average, no alert
    assert _evaluate_alert_rules(p, 100.0, None, _FakeHistory(rows)) is None


# ── state merging ──────────────────────────────────────────────────────────

def test_apply_to_state_preserves_group_key_and_merges_identifiers():
    result = CheckResult(
        product=_product(),
        price=42.0,
        selector_source="auto",
        currency="CAD",
        currency_source="detected",
        base_currency="CAD",
        price_in_base=42.0,
        identifiers={"sku": "NEW-SKU"},
    )
    state = {
        "https://example.com/w": {
            "current_price": 50.0,
            "group_key": "manual:abc123",
            "identifiers": {"mpn": "OLD-MPN"},
            "last_notification_sent": "2026-01-01T00:00:00+00:00",
        }
    }
    rec = PriceCheckService.apply_to_state(state, result)
    assert rec["current_price"] == 42.0
    assert rec["last_price"] == 50.0
    # Fields the check doesn't produce survive the merge
    assert rec["group_key"] == "manual:abc123"
    assert rec["last_notification_sent"] == "2026-01-01T00:00:00+00:00"
    # Identifiers merge key-wise instead of being replaced
    assert rec["identifiers"] == {"mpn": "OLD-MPN", "sku": "NEW-SKU"}


def test_apply_to_state_first_check_sets_last_price_to_price():
    result = CheckResult(
        product=_product(), price=10.0, currency="CAD",
        base_currency="CAD", price_in_base=10.0,
    )
    state = {}
    rec = PriceCheckService.apply_to_state(state, result)
    assert rec["last_price"] == 10.0
    assert rec["last_checked"].endswith("+00:00")  # UTC with offset


# ── failure recording ──────────────────────────────────────────────────────

def test_service_records_failure_and_recent_success_batch(tmp_path):
    history = PriceHistory(str(tmp_path / "h.db"))
    url = "https://example.com/w"
    history.record_price(url, "Widget", 10.0, status="success")
    history.record_price(url, "Widget", None, status="failed")
    history.record_price(url, "Widget", 11.0, status="success")

    # Last 2 checks are not all successes when the window includes the failure
    assert history.get_recent_success_batch([url], n=3) == {url: False}
    history.record_price(url, "Widget", 12.0, status="success")
    history.record_price(url, "Widget", 13.0, status="success")
    assert history.get_recent_success_batch([url], n=3) == {url: True}
    # Unknown URLs report False rather than being omitted
    assert history.get_recent_success_batch(["https://none"], n=3) == {"https://none": False}


class _NoNetworkExtractor:
    def __init__(self, result):
        self._result = result

    def extract(self, url, selector="", default_currency="CAD"):
        return self._result


def test_service_check_failure_records_history(tmp_path):
    from sale_monitor.services.price_extractor import ExtractionResult

    history = PriceHistory(str(tmp_path / "h.db"))
    svc = PriceCheckService(
        user_agent="t", history=history,
        config_file=str(tmp_path / "config.json"),
        extractor_factory=lambda: _NoNetworkExtractor(ExtractionResult(price=None)),
    )
    result = svc.check(_product())
    assert not result.success
    stats = history.get_failure_stats("https://example.com/w")
    assert stats["failed_checks"] == 1


def test_failed_checks_store_null_price(tmp_path):
    history = PriceHistory(str(tmp_path / "h.db"))
    url = "https://example.com/w"
    history.record_price(url, "Widget", None, status="failed")
    rows = history.get_history_extended(url)
    assert rows[0][1] is None  # not 0


def test_get_consecutive_failures(tmp_path):
    history = PriceHistory(str(tmp_path / "h.db"))
    url = "https://example.com/w"
    assert history.get_consecutive_failures(url) == 0
    history.record_price(url, "W", 10.0, status="success")
    history.record_price(url, "W", None, status="failed")
    history.record_price(url, "W", None, status="failed")
    assert history.get_consecutive_failures(url) == 2
    history.record_price(url, "W", 11.0, status="success")
    assert history.get_consecutive_failures(url) == 0


def test_failure_notification_fires_once_at_threshold(tmp_path, monkeypatch):
    """The failure email fires exactly when the streak crosses the threshold."""
    from unittest.mock import MagicMock

    from sale_monitor.cli.main import check_prices
    from sale_monitor.services.price_extractor import ExtractionResult
    from sale_monitor.storage.product_store import ProductStore

    monkeypatch.setenv("FAILURE_ALERT_CONSECUTIVE", "3")
    db = str(tmp_path / "h.db")
    history = PriceHistory(db)
    store = ProductStore(db)
    store.add(_product())

    svc = PriceCheckService(
        user_agent="t", history=history,
        config_file=str(tmp_path / "config.json"),
        extractor_factory=lambda: _NoNetworkExtractor(ExtractionResult(price=None)),
    )
    args = SimpleNamespace(
        state_file=str(tmp_path / "state.json"),
        history_db=db,
        default_cooldown_hours=24,
    )
    smtp_cfg = SimpleNamespace(enable=True)
    notifier = MagicMock()

    for _ in range(5):
        check_prices(args, smtp_cfg, notifier, svc, history=history, store=store)

    # Fired at streak == 3 only, not on streaks 4 and 5
    assert notifier.send_failure_notification.call_count == 1
    _, kwargs = notifier.send_failure_notification.call_args
    args_pos = notifier.send_failure_notification.call_args.args
    assert (args_pos and args_pos[0] == "Widget") or kwargs.get("product_name") == "Widget"
