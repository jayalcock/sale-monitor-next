import os
from unittest.mock import patch


HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def write_products_csv(path, rows):
    lines = [
        ",".join(map(lambda v: "" if v is None else str(v), r)) for r in rows
    ]
    path.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def make_client(tmp_path):
    data_dir = tmp_path
    products_csv = data_dir / "products.csv"
    state_file = data_dir / "state.json"
    history_db = data_dir / "history.db"

    write_products_csv(
        products_csv,
        [
            [
                "Widget",
                "https://example.com/w",
                "",
                "",
                "#price",
                "true",
                24,
            ]
        ],
    )
    state_file.write_text("{}", encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(state_file)
    os.environ["HISTORY_DB"] = str(history_db)

    # Import lazily to avoid linter import path issues
    from sale_monitor.web.app import create_app
    app = create_app()
    return app.test_client()


def test_pages_return_200(tmp_path):
    client = make_client(tmp_path)
    for path in ("/", "/manage", "/alerts", "/product/detail"):
        resp = client.get(path)
        assert resp.status_code == 200


def test_products_list_and_add_duplicate(tmp_path):
    client = make_client(tmp_path)

    # initial list has the seeded product
    r0 = client.get("/api/products")
    assert r0.status_code == 200
    data0 = r0.get_json()
    urls0 = {p["url"] for p in data0}
    assert "https://example.com/w" in urls0

    # add a second product
    r1 = client.post(
        "/api/product/add",
        json={
            "name": "Gadget",
            "url": "https://example.com/g",
            "selector": "#price",
            "target_price": "19.99",
            "notification_cooldown_hours": "12",
        },
    )
    assert r1.status_code == 200
    assert r1.get_json()["success"] is True

    # duplicate URL rejected
    r2 = client.post(
        "/api/product/add",
        json={
            "name": "Duplicate",
            "url": "https://example.com/g",
            "selector": "#price",
        },
    )
    assert r2.status_code == 400


def test_product_toggle_update_delete(tmp_path):
    client = make_client(tmp_path)

    # toggle existing product
    r = client.post("/api/product/toggle", json={"url": "https://example.com/w"})
    assert r.status_code == 200
    assert isinstance(r.get_json().get("enabled"), bool)

    # toggle unknown -> 404
    r2 = client.post("/api/product/toggle", json={"url": "https://example.com/missing"})
    assert r2.status_code == 404

    # add a product then update fields
    add = client.post(
        "/api/product/add",
        json={
            "name": "Thing",
            "url": "https://example.com/t",
            "selector": "#p",
            "target_price": "10.0",
            "discount_threshold": "15",
            "notification_cooldown_hours": "6",
        },
    )
    assert add.status_code == 200

    upd = client.post(
        "/api/product/update",
        json={
            "url": "https://example.com/t",
            "target_price": "12.5",
            "discount_threshold": "20",
            "notification_cooldown_hours": "8",
        },
    )
    assert upd.status_code == 200
    body = upd.get_json()
    assert body["success"] is True
    assert body["product"]["notification_cooldown_hours"] == 8

    # delete
    dele = client.post("/api/product/delete", json={"url": "https://example.com/t"})
    assert dele.status_code == 200

    # delete again -> 404
    dele2 = client.post("/api/product/delete", json={"url": "https://example.com/t"})
    assert dele2.status_code == 404


@patch("sale_monitor.services.price_extractor.PriceExtractor.extract_price", return_value=9.99)
def test_manual_check_updates_state_and_history(_mock_extract, tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/product/check", json={"url": "https://example.com/w"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["price"] == 9.99

    # Verify history export contains our record
    csv_resp = client.get("/api/export/history")
    assert csv_resp.status_code == 200
    text = csv_resp.get_data(as_text=True)
    assert "product_name,product_url,price,timestamp,status" in text
    assert "https://example.com/w" in text
    assert ",9.99," in text


def test_stats_shape_with_history(tmp_path):
    client = make_client(tmp_path)
    # Populate history by mocking two manual checks
    with patch(
        "sale_monitor.services.price_extractor.PriceExtractor.extract_price",
        return_value=10.0,
    ):
        client.post("/api/product/check", json={"url": "https://example.com/w"})
    with patch(
        "sale_monitor.services.price_extractor.PriceExtractor.extract_price",
        return_value=8.0,
    ):
        client.post("/api/product/check", json={"url": "https://example.com/w"})

    r = client.get("/api/product/stats", query_string={"url": "https://example.com/w"})
    assert r.status_code == 200
    stats = r.get_json()
    assert set(stats.keys()) >= {
        "min_price",
        "max_price",
        "avg_price",
        "current_price",
        "checks_count",
    }
    assert stats["min_price"] <= stats["max_price"]


def test_alerts_target_met(tmp_path):
    # Prepare client and then write state to simulate a target met alert
    client = make_client(tmp_path)

    # Lower the target price for Widget by updating product
    upd = client.post(
        "/api/product/update",
        json={
            "url": "https://example.com/w",
            "target_price": "20.0",
        },
    )
    assert upd.status_code == 200

    # Write state: current_price below target
    state_path = os.environ["STATE_FILE"]
    with open(state_path, "w", encoding="utf-8") as f:
        f.write(
            '{"https://example.com/w": {"current_price": 9.0, "last_checked": "2024-01-01T00:00:00", "last_price": 10.0}}'
        )

    r = client.get("/api/alerts")
    assert r.status_code == 200
    alerts = r.get_json()
    assert any(a["url"] == "https://example.com/w" and a["alert_type"] == "target_met" for a in alerts)


@patch("sale_monitor.services.price_extractor.PriceExtractor.extract_price", return_value=11.11)
def test_history_all_endpoint(_mock_extract, tmp_path):
    client = make_client(tmp_path)
    # create one history point
    client.post("/api/product/check", json={"url": "https://example.com/w"})

    r = client.get("/api/history/all", query_string={"days": 30})
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body, list)
    assert any(item["url"] == "https://example.com/w" and len(item.get("series", [])) >= 1 for item in body)


@patch("sale_monitor.services.price_extractor.PriceExtractor.extract_price", return_value=5.55)
def test_history_all_deduplicates_by_url(_mock_extract, tmp_path):
    client = make_client(tmp_path)
    url = "https://example.com/w"
    # first record with initial name
    client.post("/api/product/check", json={"url": url})
    # rename product and record again
    client.post("/api/product/update", json={"url": url, "name": "Widget 2"})
    client.post("/api/product/check", json={"url": url})

    r = client.get("/api/history/all")
    assert r.status_code == 200
    data = r.get_json()
    # only one dataset for the URL
    items_for_url = [it for it in data if it["url"] == url]
    assert len(items_for_url) == 1
    assert len(items_for_url[0].get("series", [])) >= 2
