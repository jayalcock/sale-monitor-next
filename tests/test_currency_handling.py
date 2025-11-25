from sale_monitor.storage.price_history import PriceHistory  # type: ignore


def test_currency_normalization(tmp_path):
    """Test that currency codes are properly normalized to uppercase."""
    db_path = tmp_path / "hist.db"
    h = PriceHistory(str(db_path))

    # Test currency normalization (lowercase -> uppercase)
    h.record_price(
        product_url="https://x/item",
        product_name="X",
        price=100.0,
        currency="usd",
    )

    recs = h.get_history_extended("https://x/item")
    assert len(recs) == 1
    _ts, price, _status, currency, _price_cad = recs[0]
    assert currency == "USD"
    assert price == 100.0

    # Test CAD currency
    h.record_price(
        product_url="https://y/item",
        product_name="Y",
        price=50.0,
        currency="CAD",
    )
    recs2 = h.get_history_extended("https://y/item")
    assert len(recs2) == 1
    _, price2, _status2, currency2, _price_cad2 = recs2[0]
    assert currency2 == "CAD"
    assert price2 == 50.0
