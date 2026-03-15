"""Integration tests: full web workflow (add → check → view history → delete)."""
import os
from unittest.mock import patch

HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def make_client(tmp_path):
    data_dir = tmp_path
    products_csv = data_dir / "products.csv"
    state_file = data_dir / "state.json"
    history_db = data_dir / "history.db"

    products_csv.write_text(HEADER, encoding="utf-8")
    state_file.write_text("{}", encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(state_file)
    os.environ["HISTORY_DB"] = str(history_db)
    os.environ.pop("API_KEY", None)
    os.environ.pop("API_KEY_READ_REQUIRED", None)

    from sale_monitor.web.app import create_app
    app = create_app()
    return app.test_client()


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price",
    return_value=(42.99, "manual"),
)
def test_full_product_lifecycle(_mock, tmp_path):
    """Add product → check price → view history → delete product."""
    client = make_client(tmp_path)
    url = "https://example.com/integration"

    # 1. Add product
    add = client.post(
        "/api/product/add",
        json={"name": "Integration Widget", "url": url, "selector": "#p"},
    )
    assert add.status_code == 200
    assert add.get_json()["success"] is True

    # 2. Check price
    check = client.post("/api/product/check", json={"url": url})
    assert check.status_code == 200
    assert check.get_json()["price"] == 42.99

    # 3. View products — should show last_checked and current_price
    products = client.get("/api/products").get_json()["items"]
    prod = next(p for p in products if p["url"] == url)
    assert prod["current_price"] == 42.99
    assert prod["last_checked"] is not None

    # 4. View history
    hist = client.get(f"/api/product/history?url={url}").get_json()
    assert len(hist) >= 1
    assert hist[0]["price"] == 42.99

    # 5. Delete product
    delete = client.post("/api/product/delete", json={"url": url})
    assert delete.status_code == 200

    # 6. Product no longer in list
    products_after = client.get("/api/products").get_json()["items"]
    assert not any(p["url"] == url for p in products_after)


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price",
    return_value=(99.00, "manual"),
)
def test_bulk_check_updates_all_products(_mock, tmp_path):
    client = make_client(tmp_path)

    # Add 3 products
    for i in range(3):
        client.post(
            "/api/product/add",
            json={"name": f"P{i}", "url": f"https://example.com/{i}", "selector": "#p"},
        )

    # Bulk check
    resp = client.post("/api/products/check-all")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["updated"] == 3

    # All should have prices now
    items = client.get("/api/products").get_json()["items"]
    for p in items:
        assert p["current_price"] == 99.00


@patch(
    "sale_monitor.services.price_extractor.PriceExtractor.extract_price",
    return_value=(15.00, "manual"),
)
def test_update_preserves_history(_mock, tmp_path):
    client = make_client(tmp_path)
    url = "https://example.com/hist"

    client.post("/api/product/add", json={"name": "H", "url": url, "selector": "#p"})
    client.post("/api/product/check", json={"url": url})

    # Update name
    client.post("/api/product/update", json={"url": url, "name": "H Updated"})

    # Check again
    client.post("/api/product/check", json={"url": url})

    # History should have 2 entries
    hist = client.get(f"/api/product/history?url={url}").get_json()
    assert len(hist) >= 2
