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

        price = price_extractor.extract_price(url, selector)
        assert price == 19.99

    def test_extract_price_invalid_selector(self, price_extractor, requests_mock):
        url = "http://example.com/product"
        selector = ".non-existent"
        requests_mock.get(url, text='<div class="price">$19.99</div>')

        price = price_extractor.extract_price(url, selector)
        assert price is None

    def test_extract_price_request_failure(self, price_extractor, requests_mock):
        url = "http://example.com/product"
        selector = ".price"
        requests_mock.get(url, status_code=404)

        price = price_extractor.extract_price(url, selector)
        assert price is None

    def test_parse_price_valid(self, price_extractor):
        assert price_extractor._parse_price("$19.99") == 19.99
        assert price_extractor._parse_price("€19,99") == 19.99
        assert price_extractor._parse_price("£1,234.56") == 1234.56

    def test_parse_price_invalid(self, price_extractor):
        assert price_extractor._parse_price("invalid") is None
        assert price_extractor._parse_price("") is None
        assert price_extractor._parse_price("N/A") is None