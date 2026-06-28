"""Tests for /api/compare/suggest — fuzzy pair-suggestion heuristic.

Regression: two clearly-distinct SKUs ("Switch Pro Max 16 PoE" vs
"Switch Pro Max 24 PoE") were being suggested as a link pair because their
name strings are 90%+ similar character-wise. Fix: reject pairs whose
numeric tokens (model numbers, sizes, version numbers) don't match.
"""
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


def _suggest_items(client):
    resp = client.get("/api/compare/suggest")
    data = resp.get_json()
    return data["items"] if isinstance(data, dict) else data


def test_rejects_pair_with_mismatched_model_numbers(tmp_path):
    """The exact regression: 16-port vs 24-port switches must not be paired."""
    csv = HEADER + (
        "Switch Pro Max 16 PoE,https://ca.store.ui.com/p/usw-pro-max-16-poe,,,,true,24,,CAD,\n"
        "Switch Pro Max 24 PoE,https://ca.store.ui.com/p/usw-pro-max-24-poe,,,,true,24,,CAD,\n"
    )
    items = _suggest_items(make_client(tmp_path, csv))
    assert items == []


def test_rejects_pair_with_mismatched_version_numbers(tmp_path):
    """v2 vs v3 are different products even with otherwise-identical names."""
    csv = HEADER + (
        "WidgetCo Pro v2,https://shop.example.com/widget-v2,,,,true,24,,CAD,\n"
        "WidgetCo Pro v3,https://shop.example.com/widget-v3,,,,true,24,,CAD,\n"
    )
    items = _suggest_items(make_client(tmp_path, csv))
    assert items == []


def test_pairs_same_product_at_different_vendors(tmp_path):
    """Identical product names from different vendors SHOULD still pair."""
    csv = HEADER + (
        "AKG K712 Pro Headphones,https://shop-a.com/akg-k712-pro,,,,true,24,,CAD,\n"
        "AKG K712 Pro Headphones,https://shop-b.com/akg-k712-pro,,,,true,24,,CAD,\n"
    )
    items = _suggest_items(make_client(tmp_path, csv))
    assert len(items) == 1
    pair = items[0]
    assert pair["nameA"] == "AKG K712 Pro Headphones"
    assert pair["nameB"] == "AKG K712 Pro Headphones"


def test_pairs_same_product_with_minor_punctuation_diff(tmp_path):
    """Same model, slight punctuation/casing diff — still pair (numeric tokens match)."""
    csv = HEADER + (
        "AKG K712 Pro Headphones,https://shop-a.com/akg-k712-pro,,,,true,24,,CAD,\n"
        "AKG K712-Pro Headphones,https://shop-b.com/akg-k712-pro,,,,true,24,,CAD,\n"
    )
    items = _suggest_items(make_client(tmp_path, csv))
    assert len(items) == 1


def test_pairs_when_neither_has_numbers(tmp_path):
    """If both names lack numeric tokens, numeric gate is a no-op (empty set == empty set)."""
    csv = HEADER + (
        "Cool Gadget Pro,https://shop-a.com/cool-gadget,,,,true,24,,CAD,\n"
        "Cool Gadget Pro,https://shop-b.com/cool-gadget,,,,true,24,,CAD,\n"
    )
    items = _suggest_items(make_client(tmp_path, csv))
    assert len(items) == 1
