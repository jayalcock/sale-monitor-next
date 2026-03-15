"""Tests for PriceAutoDetector resilience and fallback."""
from sale_monitor.services.auto_detector import PriceAutoDetector


def _det():
    return PriceAutoDetector()


# ---- Empty / invalid HTML ----

def test_empty_html():
    assert _det().detect_price("") == ("", "", 0.0)


def test_none_whitespace_html():
    assert _det().detect_price("   \n\t  ") == ("", "", 0.0)


def test_broken_html():
    assert _det().detect_price("<div<span>>>$19.99<<") == ("", "", 0.0)


def test_no_prices():
    html = "<html><body><h1>Hello world</h1></body></html>"
    assert _det().detect_price(html) == ("", "", 0.0)


# ---- Platform detection ----

def test_amazon_price():
    html = '<span class="a-price" data-a-color="price"><span class="a-offscreen">$42.99</span></span>'
    sel, platform, conf = _det().detect_price(html)
    assert platform == "amazon"
    assert conf >= 0.9


def test_shopify_sale_price():
    html = '<span class="price-item--sale">$19.99</span>'
    sel, platform, conf = _det().detect_price(html)
    assert platform == "shopify"
    assert conf >= 0.9


def test_woocommerce_price():
    html = '<span class="woocommerce-Price-amount amount">$35.00</span>'
    sel, platform, conf = _det().detect_price(html)
    assert platform == "woocommerce"
    assert conf >= 0.9


def test_generic_itemprop():
    html = '<span itemprop="price" content="29.99">$29.99</span>'
    sel, platform, conf = _det().detect_price(html)
    assert platform == "generic"
    assert conf > 0


# ---- Edge cases ----

def test_non_price_text_not_matched():
    """Selector matches but text doesn't look like a price → no match."""
    html = '<span class="price">More info</span>'
    assert _det().detect_price(html) == ("", "", 0.0)


def test_multiple_prices_picks_highest_confidence():
    """When multiple platform selectors match, highest-confidence wins."""
    html = (
        '<span itemprop="price" content="9.99">$9.99</span>'
        '<span class="woocommerce-Price-amount amount">$9.99</span>'
    )
    sel, platform, conf = _det().detect_price(html)
    assert platform == "woocommerce"
    assert conf > 0.8


def test_large_html_does_not_crash():
    """A very large HTML document shouldn't blow up the detector."""
    filler = "<div>" * 500 + '<span itemprop="price">$1.00</span>' + "</div>" * 500
    sel, platform, conf = _det().detect_price(filler)
    assert conf > 0


# ---- Relaxed fallback pass ----

def test_relaxed_fallback_matches_number_without_symbol():
    """Pass 2 (relaxed) should match a price-class element with digits but no symbol or decimal."""
    # "4299" has digits but no currency indicator and no decimal, so strict _looks_like_price rejects it
    html = '<span class="price">4299</span>'
    sel, platform, conf = _det().detect_price(html)
    assert conf > 0
    # Confidence is halved on relaxed pass
    assert conf <= 0.5


def test_relaxed_fallback_does_not_match_text_only():
    """Even relaxed mode should reject text-only (no digits)."""
    html = '<span class="price">Contact us</span>'
    assert _det().detect_price(html) == ("", "", 0.0)
