import pytest
from sale_monitor.services.price_extractor import PriceExtractor

class TestPriceExtractor:
    @pytest.fixture
    def price_extractor(self):
        return PriceExtractor(user_agent="test-agent")

    def test_extract_price_valid(self, price_extractor, requests_mock):
        url = "http://example.com/product"
        selector = ".price"
        requests_mock.get(url, text='<div class="price">$19.99</div>')

        price, source = price_extractor.extract_price(url, selector)
        assert price == 19.99
        assert source == 'manual'

    def test_extract_price_invalid_selector(self, price_extractor, requests_mock):
        url = "http://example.com/product"
        selector = ".non-existent"
        requests_mock.get(url, text='<div class="price">$19.99</div>')

        price, source = price_extractor.extract_price(url, selector)
        # Should fall back to auto-detection and find the price
        assert price == 19.99
        assert source == 'auto'

    def test_extract_price_request_failure(self, price_extractor, requests_mock):
        url = "http://example.com/product"
        selector = ".price"
        requests_mock.get(url, status_code=404)

        price, source = price_extractor.extract_price(url, selector)
        assert price is None
        assert source == ""

    def test_parse_price_valid(self, price_extractor):
        assert price_extractor._parse_price("$19.99") == 19.99
        assert price_extractor._parse_price("€19,99") == 19.99
        assert price_extractor._parse_price("£1,234.56") == 1234.56

    def test_parse_price_invalid(self, price_extractor):
        assert price_extractor._parse_price("invalid") is None
        assert price_extractor._parse_price("") is None
        assert price_extractor._parse_price("N/A") is None


class TestIdentifierExtraction:
    """Tests for _extract_identifiers_from_html and related helpers."""

    @pytest.fixture
    def extractor(self):
        return PriceExtractor(user_agent="test-agent")

    def test_extracts_from_product_jsonld(self, extractor):
        html = '''<html><head>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Widget", "sku": "W-100", "mpn": "MPN-42",
         "brand": {"@type": "Brand", "name": "Acme"}}
        </script></head><body></body></html>'''
        ids = extractor._extract_identifiers_from_html(html)
        assert ids["sku"] == "W-100"
        assert ids["mpn"] == "MPN-42"
        assert ids["brand"] == "Acme"
        assert ids["name"] == "Widget"

    def test_ignores_non_product_jsonld(self, extractor):
        """Organization and WebSite names must not leak into identifiers."""
        html = '''<html><head>
        <script type="application/ld+json">
        {"@type": "Organization", "name": "My Store Inc", "url": "https://store.com"}
        </script>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Gadget", "sku": "G-1"}
        </script></head><body></body></html>'''
        ids = extractor._extract_identifiers_from_html(html)
        assert ids["name"] == "Gadget"
        assert ids["sku"] == "G-1"

    def test_ignores_breadcrumb_jsonld(self, extractor):
        html = '''<html><head>
        <script type="application/ld+json">
        {"@type": "BreadcrumbList", "name": "Home > Bikes", "itemListElement": []}
        </script></head><body></body></html>'''
        ids = extractor._extract_identifiers_from_html(html)
        assert "name" not in ids

    def test_nested_product_in_graph(self, extractor):
        """Product inside @graph array should be found."""
        html = '''<html><head>
        <script type="application/ld+json">
        {"@graph": [
            {"@type": "WebSite", "name": "Store"},
            {"@type": "Product", "name": "Board", "mpn": "B-99",
             "offers": {"@type": "Offer", "sku": "OFF-1"}}
        ]}
        </script></head><body></body></html>'''
        ids = extractor._extract_identifiers_from_html(html)
        assert ids["name"] == "Board"
        assert ids["mpn"] == "B-99"
        assert ids["sku"] == "OFF-1"
        # Should NOT have "Store"
        assert ids["name"] != "Store"

    def test_last_identifiers_reset_between_calls(self, extractor, requests_mock):
        """last_identifiers must not carry over from a previous product."""
        import os
        os.environ['SALEMONITOR_DISABLE_HTML_CURRENCY'] = '0'
        url_a = "http://store-a.com/product-a"
        url_b = "http://store-b.com/product-b"
        html_a = '''<html><head>
        <script type="application/ld+json">
        {"@type":"Product","name":"Alpha","mpn":"MPN-A"}
        </script></head><body><span class="price">$10</span></body></html>'''
        # Product B returns 500 — no identifiers should be extracted
        requests_mock.get(url_a, text=html_a)
        requests_mock.get(url_b, status_code=500)

        extractor.extract_price_with_currency(url_a, ".price")
        assert extractor.last_identifiers.get("mpn") == "MPN-A"

        extractor.extract_price_with_currency(url_b, ".price")
        # Must be empty — not stale from product A
        assert extractor.last_identifiers == {}