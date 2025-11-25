import sys
from pathlib import Path
import pytest

# Ensure src/ is on path for static analyzers that don't run conftest
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sale_monitor.services.auto_detector import PriceAutoDetector
from sale_monitor.services.price_extractor import PriceExtractor


SHOPIFY_SALE_HTML = """
<html><body>
  <div class="price__container">
    <div class="price__regular">
      <span class="visually-hidden visually-hidden--inline">Regular price</span>
      <span class="price-item price-item--regular"><span class="money">$5,000.00 AUD</span></span>
    </div>
    <div class="price__sale">
      <span class="visually-hidden visually-hidden--inline">Regular price</span>
      <span><s class="price-item price-item--regular"><span class="money">$5,000.00 AUD</span></s></span>
      <span class="visually-hidden visually-hidden--inline">Sale price</span>
      <span class="price-item price-item--sale price-item--last"><span class="money">$4,699.00 AUD</span></span>
    </div>
  </div>
</body></html>
"""

SHOPIFY_REGULAR_ONLY_HTML = """
<html><body>
  <div class="price__container">
    <div class="price__regular">
      <span class="visually-hidden visually-hidden--inline">Regular price</span>
      <span class="price-item price-item--regular"><span class="money">$2,499.00 AUD</span></span>
    </div>
  </div>
</body></html>
"""


def test_shopify_sale_detection_prefers_sale_selector():
    det = PriceAutoDetector()
    selector, platform, confidence = det.detect_price(SHOPIFY_SALE_HTML)
    assert platform == 'shopify'
    assert selector == '.price__sale .price-item--sale'
    assert confidence > 0.9


def test_shopify_regular_detection_when_no_sale_block():
    det = PriceAutoDetector()
    selector, platform, confidence = det.detect_price(SHOPIFY_REGULAR_ONLY_HTML)
    assert platform == 'shopify'
    # Should fall back to regular price selector now that it's included
    assert selector in {'.price__regular .price-item--regular', '.price-item--regular'}
    assert confidence > 0.9


class DummyResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.headers = {'content-type': 'text/html'}


def test_price_extractor_parses_sale_price(monkeypatch):
    # Monkeypatch requests.Session.get to return our dummy sale HTML
    def fake_get(*_args, **_kwargs):
        return DummyResponse(SHOPIFY_SALE_HTML)

    monkeypatch.setattr('requests.Session.get', fake_get)
    extractor = PriceExtractor(user_agent='TestAgent', timeout=10, max_retries=1)
    price, source = extractor.extract_price('https://example.com/product', '')
    assert source == 'auto'
    assert price == pytest.approx(4699.00, rel=1e-6)


def test_price_extractor_parses_regular_price(monkeypatch):
    def fake_get(*_args, **_kwargs):
        return DummyResponse(SHOPIFY_REGULAR_ONLY_HTML)

    monkeypatch.setattr('requests.Session.get', fake_get)
    extractor = PriceExtractor(user_agent='TestAgent', timeout=10, max_retries=1)
    price, source = extractor.extract_price('https://example.com/product', '')
    assert source == 'auto'
    assert price == pytest.approx(2499.00, rel=1e-6)
