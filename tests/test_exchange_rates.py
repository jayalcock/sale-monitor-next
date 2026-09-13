"""Tests for exchange-rate fetching, caching, and staleness surfacing."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.services.price_check import CheckResult
from sale_monitor.storage.price_history import PriceHistory


def _api_response(rates):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {'result': 'success', 'rates': rates}
    return resp


class FakeHandler:
    """Cache handler with the bulk method."""

    def __init__(self):
        self.store = {}  # (base, target) -> rate
        self.bulk_calls = 0

    def cache_exchange_rates(self, base, rates):
        self.bulk_calls += 1
        for target, rate in rates.items():
            self.store[(base, target)] = rate

    def cache_exchange_rate(self, base, target, rate):
        raise AssertionError("bulk method should be preferred")

    def get_cached_rate(self, base, target, max_age_hours=24):
        return self.store.get((base, target))


# ── service unit tests ───────────────────────────────────────────────────────


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_get_rate_fetches_and_persists_bulk(mock_get):
    mock_get.return_value = _api_response({'CAD': 1.38, 'EUR': 0.85})
    handler = FakeHandler()
    svc = ExchangeRateService(cache_handler=handler)

    assert svc.get_rate('USD', 'CAD') == 1.38
    assert handler.bulk_calls == 1
    assert handler.store[('USD', 'EUR')] == 0.85
    # second call served from memory: no new API hit
    assert svc.get_rate('USD', 'EUR') == 0.85
    assert mock_get.call_count == 1


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_api_failure_falls_back_to_stale_cache(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("down")

    handler = MagicMock()
    # fresh-window lookup misses, year-window fallback hits
    handler.get_cached_rate.side_effect = (
        lambda base, target, max_age_hours=24: 1.30 if max_age_hours > 24 else None
    )
    svc = ExchangeRateService(cache_handler=handler)
    assert svc.get_rate('USD', 'CAD') == 1.30


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_missing_currency_falls_back_to_stale_cache(mock_get):
    mock_get.return_value = _api_response({'EUR': 0.85})  # no CAD
    handler = MagicMock()
    handler.get_cached_rate.side_effect = (
        lambda base, target, max_age_hours=24: 1.25 if max_age_hours > 24 else None
    )
    handler.cache_exchange_rates = MagicMock()
    svc = ExchangeRateService(cache_handler=handler)
    assert svc.get_rate('USD', 'CAD') == 1.25


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_fetch_rates_never_falls_back(mock_get):
    """fetch_rates must report failure honestly — it backs the refresh endpoint."""
    mock_get.side_effect = requests.exceptions.ConnectionError("down")
    handler = FakeHandler()
    handler.store[('USD', 'CAD')] = 1.30  # stale value available
    svc = ExchangeRateService(cache_handler=handler)

    assert svc.fetch_rates('USD') is None
    assert svc.refresh(['USD', 'EUR']) == {'EUR': False, 'USD': False}


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_broken_cache_handler_is_treated_as_miss(mock_get):
    mock_get.return_value = _api_response({'CAD': 1.38})
    handler = MagicMock()
    handler.get_cached_rate.side_effect = sqlite3.OperationalError("database is locked")
    handler.cache_exchange_rates.side_effect = sqlite3.OperationalError("database is locked")
    svc = ExchangeRateService(cache_handler=handler)

    assert svc.get_rate('USD', 'CAD') == 1.38


# ── persistent cache (PriceHistory) ─────────────────────────────────────────


def test_bulk_cache_roundtrip_and_ttl(tmp_path):
    history = PriceHistory(str(tmp_path / "h.db"))
    history.cache_exchange_rates('USD', {'CAD': 1.38, 'EUR': 0.85})
    assert history.get_cached_rate('USD', 'CAD') == 1.38

    # age the row past the default TTL
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with sqlite3.connect(str(tmp_path / "h.db")) as conn:
        conn.execute("UPDATE exchange_rates SET timestamp = ?", (old,))
        conn.commit()
    assert history.get_cached_rate('USD', 'CAD') is None
    assert history.get_cached_rate('USD', 'CAD', max_age_hours=24 * 365) == 1.38


# ── web endpoints ────────────────────────────────────────────────────────────

HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def make_client(tmp_path):
    products_csv = tmp_path / "products.csv"
    products_csv.write_text(
        HEADER + "Widget,https://example.com/w,,,#price,true,24\n", encoding="utf-8"
    )
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(tmp_path / "state.json")
    os.environ["HISTORY_DB"] = str(tmp_path / "history.db")
    os.environ["CONFIG_FILE"] = str(tmp_path / "config.json")

    from sale_monitor.web.app import create_app
    return create_app().test_client()


def _no_network_check(self, product, record_history=True):
    return CheckResult(product=product)


def _add_usd_product(client):
    with patch("sale_monitor.services.price_check.PriceCheckService.check", _no_network_check):
        r = client.post("/api/product/add", json={
            "name": "US Thing", "url": "https://example.com/us", "currency": "USD",
        })
    assert r.status_code == 200


def _age_rates(db_path, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO exchange_rates "
            "(base_currency, target_currency, rate, timestamp) VALUES (?, ?, ?, ?)",
            ('USD', 'CAD', 1.30, old),
        )
        conn.commit()


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_refresh_endpoint_reports_api_failure(mock_get, tmp_path):
    """Regression: a dead API used to fall back to the stale cache and the
    endpoint reported success, so the dashboard showed 'Fresh'."""
    mock_get.side_effect = requests.exceptions.ConnectionError("down")
    client = make_client(tmp_path)
    _add_usd_product(client)
    _age_rates(os.environ["HISTORY_DB"], days=50)  # stale rate available

    r = client.post("/api/rates/refresh")
    assert r.status_code == 502
    assert 'USD' in r.get_json()['error']


@patch('sale_monitor.services.exchange_rates.requests.get')
def test_refresh_endpoint_success(mock_get, tmp_path):
    mock_get.return_value = _api_response({'CAD': 1.38, 'EUR': 0.85})
    client = make_client(tmp_path)
    _add_usd_product(client)

    r = client.post("/api/rates/refresh")
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['refreshed'] == ['USD']


def test_health_flags_stale_rates(tmp_path):
    client = make_client(tmp_path)
    _add_usd_product(client)
    _age_rates(os.environ["HISTORY_DB"], days=50)

    body = client.get("/api/health/detailed").get_json()
    assert body['exchange_rates_in_use'] == ['USD']
    assert body['exchange_rates_stale'] is True
    assert body['exchange_rate_cache_age_seconds'] > 49 * 24 * 3600
    assert isinstance(body['rows'], int)  # rate query must not clobber the row count


def test_health_fresh_rates_not_flagged(tmp_path):
    client = make_client(tmp_path)
    _add_usd_product(client)
    _age_rates(os.environ["HISTORY_DB"], days=0)

    body = client.get("/api/health/detailed").get_json()
    assert body['exchange_rates_stale'] is False


def test_health_no_foreign_currencies(tmp_path):
    """All products in base currency: no rates in use, nothing stale."""
    client = make_client(tmp_path)
    body = client.get("/api/health/detailed").get_json()
    assert body['exchange_rates_in_use'] == []
    assert body['exchange_rates_stale'] is False
    assert body['exchange_rate_cache_age_seconds'] is None
