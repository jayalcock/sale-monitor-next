"""Tests for API pagination (Phase 3)."""
import os

HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def write_products_csv(path, rows):
    lines = [
        ",".join(map(lambda v: "" if v is None else str(v), r)) for r in rows
    ]
    path.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def make_client(tmp_path, n_products=10):
    data_dir = tmp_path
    products_csv = data_dir / "products.csv"
    state_file = data_dir / "state.json"
    history_db = data_dir / "history.db"

    rows = [
        [f"Product{i}", f"https://example.com/p{i}", "", "", "#price", "true", 24]
        for i in range(n_products)
    ]
    write_products_csv(products_csv, rows)
    state_file.write_text("{}", encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(state_file)
    os.environ["HISTORY_DB"] = str(history_db)
    os.environ.pop("API_KEY", None)
    os.environ.pop("API_KEY_READ_REQUIRED", None)

    from sale_monitor.web.app import create_app
    app = create_app()
    return app.test_client()


def test_products_returns_envelope(tmp_path):
    client = make_client(tmp_path, n_products=3)
    resp = client.get("/api/products")
    data = resp.get_json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total"] == 3
    assert len(data["items"]) == 3


def test_products_pagination_limit_offset(tmp_path):
    client = make_client(tmp_path, n_products=10)
    resp = client.get("/api/products?limit=3&offset=2")
    data = resp.get_json()
    assert data["total"] == 10
    assert data["limit"] == 3
    assert data["offset"] == 2
    assert len(data["items"]) == 3
    assert data["items"][0]["name"] == "Product2"


def test_products_pagination_limit_clamped(tmp_path):
    client = make_client(tmp_path, n_products=5)
    resp = client.get("/api/products?limit=999")
    data = resp.get_json()
    assert data["limit"] == 200  # clamped to max


def test_products_no_limit_returns_all(tmp_path):
    client = make_client(tmp_path, n_products=15)
    resp = client.get("/api/products")
    data = resp.get_json()
    assert data["total"] == 15
    assert len(data["items"]) == 15


def test_failures_returns_envelope(tmp_path):
    client = make_client(tmp_path, n_products=1)
    resp = client.get("/api/failures")
    data = resp.get_json()
    assert "items" in data
    assert "total" in data


def test_compare_groups_returns_envelope(tmp_path):
    client = make_client(tmp_path, n_products=1)
    resp = client.get("/api/compare/groups")
    data = resp.get_json()
    assert "items" in data
    assert "total" in data


def test_compare_suggest_returns_envelope(tmp_path):
    client = make_client(tmp_path, n_products=1)
    resp = client.get("/api/compare/suggest")
    data = resp.get_json()
    assert "items" in data
    assert "total" in data
