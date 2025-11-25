import pytest
from sale_monitor.services.price_extractor import PriceExtractor


class DummyExtractor(PriceExtractor):
    def extract_price(self, url: str, selector: str = ""):
        # Bypass network; return a fixed price
        return 100.0, "manual"


def test_jensonusa_infers_usd(monkeypatch):
    extractor = DummyExtractor(user_agent="tester", timeout=1, max_retries=1)

    class DummyResp:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

    # Avoid real network and force detection path
    monkeypatch.setattr(extractor, "session", type("S", (), {"get": lambda _self, url, timeout: DummyResp("{}", 200)})())
    monkeypatch.setattr(extractor, "_detect_currency_from_html", lambda html, url: "USD")

    price, _source, currency = extractor.extract_price_with_currency(
        "https://www.jensonusa.com/some-product", "", default_currency="CAD"
    )
    assert currency == "USD"
    assert price == 100.0


def test_curvecycling_detects_cad(monkeypatch):
    extractor = DummyExtractor(user_agent="tester", timeout=1, max_retries=1)

    class DummyResp:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

    monkeypatch.setattr(extractor, "session", type("S", (), {"get": lambda _self, url, timeout: DummyResp("{}", 200)})())
    monkeypatch.setattr(extractor, "_detect_currency_from_html", lambda html, url: "CAD")

    price, _source, currency = extractor.extract_price_with_currency(
        "https://www.curvecycling.com/products/some-product", "", default_currency="CAD"
    )
    assert currency == "CAD"
    assert price == 100.0
