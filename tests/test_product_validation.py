"""Tests for shared product validation and the hardened add/import paths."""
import os
from unittest.mock import patch

import pytest

from sale_monitor.domain.validation import (VALID_ALERT_RULES,
                                            ProductValidationError,
                                            normalize_url, parse_choices,
                                            parse_currency)
from sale_monitor.services.http_safety import is_obviously_non_public_url
from sale_monitor.services.price_check import CheckResult

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

    from sale_monitor.web.app import create_app
    return create_app().test_client()


def _no_network_check(self, product, record_history=True):
    return CheckResult(product=product)


# ── validation unit tests ────────────────────────────────────────────────────


def test_normalize_url_strips_and_drops_fragment():
    assert normalize_url("  https://example.com/p#reviews ") == "https://example.com/p"
    # query strings are kept
    assert normalize_url("https://example.com/p?variant=2") == "https://example.com/p?variant=2"


@pytest.mark.parametrize("bad", ["", "   ", "ftp://example.com/x", "https://", "notaurl", None])
def test_normalize_url_rejects_invalid(bad):
    with pytest.raises(ProductValidationError):
        normalize_url(bad)


def test_parse_choices_rejects_unknown_rule():
    with pytest.raises(ProductValidationError, match="pricedrop"):
        parse_choices(["target", "pricedrop"], VALID_ALERT_RULES, "alert_rules")


def test_parse_currency():
    assert parse_currency("usd") == "USD"
    assert parse_currency(None) == "CAD"
    assert parse_currency("", default="EUR") == "EUR"
    with pytest.raises(ProductValidationError):
        parse_currency("DOLLARS")


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/p", False),
    ("https://shop.example.co.uk/item", False),
    ("http://192.168.1.1/admin", True),
    ("http://127.0.0.1:8080/x", True),
    ("http://[::1]/x", True),
    ("http://localhost/x", True),
    ("http://intranet/x", True),  # single-label host
    ("http://169.254.169.254/latest/meta-data", True),  # cloud metadata
])
def test_is_obviously_non_public_url(url, expected):
    assert is_obviously_non_public_url(url) is expected


# ── web route behavior ───────────────────────────────────────────────────────


@patch("sale_monitor.services.price_check.PriceCheckService.check", _no_network_check)
def test_add_normalizes_url_and_catches_duplicates(tmp_path):
    client = make_client(tmp_path)
    r1 = client.post("/api/product/add", json={
        "name": "  Gadget  ",
        "url": "  https://example.com/g#reviews ",
    })
    assert r1.status_code == 200
    assert r1.get_json()["product"]["url"] == "https://example.com/g"
    assert r1.get_json()["product"]["name"] == "Gadget"

    # the same URL with cosmetic differences is a duplicate now
    r2 = client.post("/api/product/add", json={
        "name": "Dup",
        "url": "https://example.com/g#other-fragment",
    })
    assert r2.status_code == 400
    assert "exists" in r2.get_json()["error"]


@patch("sale_monitor.services.price_check.PriceCheckService.check", _no_network_check)
def test_add_rejects_unknown_alert_rule_and_channel(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/product/add", json={
        "name": "X", "url": "https://example.com/x",
        "alert_rules": ["target", "pricedrop"],
    })
    assert r.status_code == 400
    assert "pricedrop" in r.get_json()["error"]

    r2 = client.post("/api/product/add", json={
        "name": "X", "url": "https://example.com/x",
        "notification_channels": ["telegram"],
    })
    assert r2.status_code == 400


@patch("sale_monitor.services.price_check.PriceCheckService.check", _no_network_check)
def test_add_accepts_currency(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/product/add", json={
        "name": "US Thing", "url": "https://example.com/us", "currency": "usd",
    })
    assert r.status_code == 200
    assert r.get_json()["product"]["currency"] == "USD"

    products = client.get("/api/products").get_json()["items"]
    us = next(p for p in products if p["url"] == "https://example.com/us")
    assert us["configured_currency"] == "USD"


@patch("sale_monitor.services.price_check.PriceCheckService.check", _no_network_check)
def test_add_rejects_private_hosts(tmp_path):
    client = make_client(tmp_path)
    for url in ("http://127.0.0.1/x", "http://192.168.1.10/x", "http://localhost/x"):
        r = client.post("/api/product/add", json={"name": "SSRF", "url": url})
        assert r.status_code == 400, url
        assert "public" in r.get_json()["error"]

    r = client.post("/api/product/add", json={
        "name": "SSRF", "url": "https://example.com/ok",
        "scrape_url": "http://169.254.169.254/latest/meta-data",
    })
    assert r.status_code == 400
    assert "scrape_url" in r.get_json()["error"]


def test_bulk_import_bad_row_does_not_abort(tmp_path):
    """Regression: a bad cooldown used to raise mid-loop, aborting the import
    after some rows were already inserted."""
    client = make_client(tmp_path)
    r = client.post("/api/products/bulk-import", json={"products": [
        {"name": "A", "url": "https://example.com/a"},
        {"name": "B", "url": "https://example.com/b", "notification_cooldown_hours": "lots"},
        {"name": "C", "url": "https://example.com/c", "currency": "USD"},
    ]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["added"] == 2
    assert body["added_names"] == ["A", "C"]
    assert len(body["errors"]) == 1
    assert "B" in body["errors"][0]


def test_update_validates_like_add(tmp_path):
    client = make_client(tmp_path)
    url = "https://example.com/w"  # seeded product

    r = client.post("/api/product/update", json={"url": url, "target_price": "-3"})
    assert r.status_code == 400
    assert "negative" in r.get_json()["error"]

    r2 = client.post("/api/product/update", json={"url": url, "notification_cooldown_hours": "9999"})
    assert r2.status_code == 400
    assert "8760" in r2.get_json()["error"]

    r3 = client.post("/api/product/update", json={"url": url, "currency": "eur"})
    assert r3.status_code == 200
    assert r3.get_json()["product"]["currency"] == "EUR"


def test_csv_import_skips_invalid_rows(tmp_path):
    from sale_monitor.storage.product_store import ProductStore

    csv_path = tmp_path / "import.csv"
    csv_path.write_text(
        HEADER
        + "Good,https://example.com/good,,,#p,true,24\n"
        + "BadScheme,ftp://example.com/bad,,,#p,true,24\n"
        + "Good2,https://example.com/good2,,,#p,true,24\n",
        encoding="utf-8",
    )
    store = ProductStore(str(tmp_path / "import.db"))
    assert store.import_from_csv(str(csv_path)) == 2
    assert store.urls() == {"https://example.com/good", "https://example.com/good2"}
