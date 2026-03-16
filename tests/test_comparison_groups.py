"""Tests for competitive comparison groups (CSV group, manual link, auto-detect)."""
import json
import os


def make_client(tmp_path, csv_text, state=None):
    products_csv = tmp_path / "products.csv"
    state_file = tmp_path / "state.json"
    history_db = tmp_path / "history.db"

    products_csv.write_text(csv_text, encoding="utf-8")
    state_file.write_text(json.dumps(state or {}), encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(state_file)
    os.environ["HISTORY_DB"] = str(history_db)
    os.environ.pop("API_KEY", None)
    os.environ.pop("API_KEY_READ_REQUIRED", None)

    from sale_monitor.web.app import create_app
    app = create_app()
    return app.test_client()


HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours,selector_source,currency,group\n"


def test_csv_group_creates_competitive_group(tmp_path):
    """Products sharing the same CSV group column form a group."""
    csv = HEADER + (
        "Widget A,https://vendor-a.com/widget,,,#p,true,24,,CAD,my-widget\n"
        "Widget B,https://vendor-b.com/widget,,,#p,true,24,,USD,my-widget\n"
        "Other,https://vendor-c.com/other,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://vendor-a.com/widget": {"current_price": 100.0, "currency": "CAD", "price_in_base": 100.0},
        "https://vendor-b.com/widget": {"current_price": 80.0, "currency": "USD", "price_in_base": 110.0},
        "https://vendor-c.com/other": {"current_price": 50.0, "currency": "CAD", "price_in_base": 50.0},
    }
    client = make_client(tmp_path, csv, state)
    resp = client.get("/api/compare/groups")
    data = resp.get_json()
    groups = data["items"]

    assert len(groups) == 1
    g = groups[0]
    assert g["group_key"] == "my-widget"
    assert g["source"] == "csv"
    assert len(g["items"]) == 2
    urls = {it["url"] for it in g["items"]}
    assert urls == {"https://vendor-a.com/widget", "https://vendor-b.com/widget"}


def test_csv_group_takes_precedence_over_auto_identifiers(tmp_path):
    """CSV group wins even when products share auto-detected identifiers."""
    csv = HEADER + (
        "Widget A,https://vendor-a.com/w,,,#p,true,24,,CAD,explicit-group\n"
        "Widget B,https://vendor-b.com/w,,,#p,true,24,,CAD,explicit-group\n"
    )
    # Both have the same MPN but CSV group should take priority
    state = {
        "https://vendor-a.com/w": {
            "current_price": 100.0, "currency": "CAD", "price_in_base": 100.0,
            "identifiers": {"mpn": "SHARED-MPN"},
        },
        "https://vendor-b.com/w": {
            "current_price": 90.0, "currency": "CAD", "price_in_base": 90.0,
            "identifiers": {"mpn": "SHARED-MPN"},
        },
    }
    client = make_client(tmp_path, csv, state)
    groups = client.get("/api/compare/groups").get_json()["items"]

    assert len(groups) == 1
    assert groups[0]["group_key"] == "explicit-group"
    assert groups[0]["source"] == "csv"


def test_auto_identifier_grouping_still_works(tmp_path):
    """Products without CSV group still form groups via shared MPN."""
    csv = HEADER + (
        "Widget A,https://a.com/w,,,#p,true,24,,CAD,\n"
        "Widget B,https://b.com/w,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://a.com/w": {
            "current_price": 100.0, "currency": "CAD", "price_in_base": 100.0,
            "identifiers": {"mpn": "MPN-123"},
        },
        "https://b.com/w": {
            "current_price": 90.0, "currency": "CAD", "price_in_base": 90.0,
            "identifiers": {"mpn": "MPN-123"},
        },
    }
    client = make_client(tmp_path, csv, state)
    groups = client.get("/api/compare/groups").get_json()["items"]

    assert len(groups) == 1
    assert groups[0]["group_key"] == "MPN-123"
    assert groups[0]["source"] == "auto"


def test_manual_group_key_overrides_auto(tmp_path):
    """Manual group_key in state takes precedence over auto identifiers."""
    csv = HEADER + (
        "Widget A,https://a.com/w,,,#p,true,24,,CAD,\n"
        "Widget B,https://b.com/w,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://a.com/w": {
            "current_price": 100.0, "currency": "CAD", "price_in_base": 100.0,
            "group_key": "manual:abc",
            "identifiers": {"sku": "SKU-A"},
        },
        "https://b.com/w": {
            "current_price": 90.0, "currency": "CAD", "price_in_base": 90.0,
            "group_key": "manual:abc",
            "identifiers": {"sku": "SKU-B"},
        },
    }
    client = make_client(tmp_path, csv, state)
    groups = client.get("/api/compare/groups").get_json()["items"]

    assert len(groups) == 1
    assert groups[0]["group_key"] == "manual:abc"
    assert groups[0]["source"] == "manual"


def test_single_product_group_not_shown(tmp_path):
    """A group with only one product is not returned."""
    csv = HEADER + (
        "Widget A,https://a.com/w,,,#p,true,24,,CAD,lonely-group\n"
        "Other,https://b.com/o,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://a.com/w": {"current_price": 100.0, "currency": "CAD", "price_in_base": 100.0},
        "https://b.com/o": {"current_price": 50.0, "currency": "CAD", "price_in_base": 50.0},
    }
    client = make_client(tmp_path, csv, state)
    groups = client.get("/api/compare/groups").get_json()["items"]
    assert len(groups) == 0


def test_group_items_sorted_by_price(tmp_path):
    """Items within a group are sorted lowest price first."""
    csv = HEADER + (
        "Expensive,https://a.com/w,,,#p,true,24,,CAD,widgets\n"
        "Cheap,https://b.com/w,,,#p,true,24,,CAD,widgets\n"
        "Mid,https://c.com/w,,,#p,true,24,,CAD,widgets\n"
    )
    state = {
        "https://a.com/w": {"current_price": 300.0, "currency": "CAD", "price_in_base": 300.0},
        "https://b.com/w": {"current_price": 100.0, "currency": "CAD", "price_in_base": 100.0},
        "https://c.com/w": {"current_price": 200.0, "currency": "CAD", "price_in_base": 200.0},
    }
    client = make_client(tmp_path, csv, state)
    groups = client.get("/api/compare/groups").get_json()["items"]

    assert len(groups) == 1
    prices = [it["price_in_base"] for it in groups[0]["items"]]
    assert prices == [100.0, 200.0, 300.0]


def test_api_products_returns_group_field(tmp_path):
    """The /api/products endpoint includes the group field."""
    csv = HEADER + "Widget,https://a.com/w,,,#p,true,24,,CAD,my-group\n"
    state = {"https://a.com/w": {"current_price": 50.0, "currency": "CAD"}}
    client = make_client(tmp_path, csv, state)
    products = client.get("/api/products").get_json()["items"]

    assert len(products) == 1
    assert products[0]["group"] == "my-group"


def test_api_products_group_null_when_empty(tmp_path):
    """Products without a group return group=None."""
    csv = HEADER + "Widget,https://a.com/w,,,#p,true,24,,CAD,\n"
    state = {"https://a.com/w": {"current_price": 50.0, "currency": "CAD"}}
    client = make_client(tmp_path, csv, state)
    products = client.get("/api/products").get_json()["items"]

    assert products[0]["group"] is None


def test_manual_link_merges_existing_group(tmp_path):
    """Linking B↔C when B already belongs to group with A merges all three."""
    csv = HEADER + (
        "A,https://a.com/w,,,#p,true,24,,CAD,\n"
        "B,https://b.com/w,,,#p,true,24,,CAD,\n"
        "C,https://c.com/w,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://a.com/w": {"current_price": 10.0, "currency": "CAD", "price_in_base": 10.0, "group_key": "manual:existing"},
        "https://b.com/w": {"current_price": 20.0, "currency": "CAD", "price_in_base": 20.0, "group_key": "manual:existing"},
        "https://c.com/w": {"current_price": 30.0, "currency": "CAD", "price_in_base": 30.0},
    }
    client = make_client(tmp_path, csv, state)

    # Link B↔C — C should join the existing group that A and B are in
    resp = client.post("/api/compare/link", json={"urlA": "https://b.com/w", "urlB": "https://c.com/w"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["group_key"] == "manual:existing"

    # All three should be in one group
    groups = client.get("/api/compare/groups").get_json()["items"]
    manual_groups = [g for g in groups if g["source"] == "manual"]
    assert len(manual_groups) == 1
    urls = {it["url"] for it in manual_groups[0]["items"]}
    assert urls == {"https://a.com/w", "https://b.com/w", "https://c.com/w"}


def test_manual_link_merges_two_existing_groups(tmp_path):
    """Linking products from two different groups merges all members."""
    csv = HEADER + (
        "A,https://a.com/w,,,#p,true,24,,CAD,\n"
        "B,https://b.com/w,,,#p,true,24,,CAD,\n"
        "C,https://c.com/w,,,#p,true,24,,CAD,\n"
        "D,https://d.com/w,,,#p,true,24,,CAD,\n"
    )
    state = {
        "https://a.com/w": {"current_price": 10.0, "currency": "CAD", "price_in_base": 10.0, "group_key": "manual:group1"},
        "https://b.com/w": {"current_price": 20.0, "currency": "CAD", "price_in_base": 20.0, "group_key": "manual:group1"},
        "https://c.com/w": {"current_price": 30.0, "currency": "CAD", "price_in_base": 30.0, "group_key": "manual:group2"},
        "https://d.com/w": {"current_price": 40.0, "currency": "CAD", "price_in_base": 40.0, "group_key": "manual:group2"},
    }
    client = make_client(tmp_path, csv, state)

    # Link A↔C — should merge group1 and group2
    resp = client.post("/api/compare/link", json={"urlA": "https://a.com/w", "urlB": "https://c.com/w"})
    assert resp.status_code == 200

    groups = client.get("/api/compare/groups").get_json()["items"]
    manual_groups = [g for g in groups if g["source"] == "manual"]
    assert len(manual_groups) == 1
    urls = {it["url"] for it in manual_groups[0]["items"]}
    assert urls == {"https://a.com/w", "https://b.com/w", "https://c.com/w", "https://d.com/w"}
