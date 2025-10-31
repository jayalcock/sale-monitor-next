"""
Auto-detection of price selectors for common e-commerce platforms.
"""
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup


class PriceAutoDetector:
    """Attempts to automatically detect price elements on product pages."""
    
    # Common price element patterns (selector, confidence_weight)
    PATTERNS: List[Tuple[str, str, float]] = [
        # Shopify
        ('.product-single__price .money', 'shopify', 0.95),
        ('.product__price .money', 'shopify', 0.95),
        ('[data-product-price]', 'shopify', 0.90),
        ('.price--main', 'shopify', 0.90),
        ('.product-price', 'shopify', 0.85),
        
        # WooCommerce
        ('.woocommerce-Price-amount.amount', 'woocommerce', 0.95),
        ('p.price ins .woocommerce-Price-amount', 'woocommerce', 0.95),
        ('p.price .amount', 'woocommerce', 0.90),
        ('.summary .price', 'woocommerce', 0.85),
        
        # BigCommerce
        ('.productView-price', 'bigcommerce', 0.95),
        ('[data-product-price]', 'bigcommerce', 0.90),
        
        # Magento
        ('[data-price-type="finalPrice"]', 'magento', 0.95),
        ('.price-box .price', 'magento', 0.90),
        ('.product-info-price .price', 'magento', 0.85),
        
        # Generic patterns (lower confidence)
        ('[itemprop="price"]', 'generic', 0.80),
        ('[data-price]', 'generic', 0.75),
        ('.price', 'generic', 0.60),
        ('#price', 'generic', 0.60),
        ('.product-price', 'generic', 0.70),
        ('.current-price', 'generic', 0.75),
        ('.sale-price', 'generic', 0.70),
        ('.final-price', 'generic', 0.70),
    ]
    
    def __init__(self):
        self.last_detected_platform: Optional[str] = None
        self.last_detected_selector: Optional[str] = None
        self.last_confidence: Optional[float] = None
    
    def detect_price(self, html: str) -> Optional[Tuple[str, str, float]]:
        """
        Try to automatically detect the price selector from HTML.
        
        Args:
            html: Raw HTML content of the product page
            
        Returns:
            Tuple of (selector, platform, confidence) if found, None otherwise
        """
        soup = BeautifulSoup(html, 'html.parser')
        
        best_match = None
        best_confidence = 0.0
        
        for selector, platform, confidence in self.PATTERNS:
            try:
                elements = soup.select(selector)
                if elements:
                    # Check if the element contains price-like text
                    for elem in elements:
                        text = elem.get_text(strip=True)
                        if self._looks_like_price(text):
                            if confidence > best_confidence:
                                best_match = (selector, platform, confidence)
                                best_confidence = confidence
                            break
            except Exception:
                # Invalid selector or parsing error, skip
                continue
        
        if best_match:
            self.last_detected_selector = best_match[0]
            self.last_detected_platform = best_match[1]
            self.last_confidence = best_match[2]
        
        return best_match
    
    @staticmethod
    def _looks_like_price(text: str) -> bool:
        """Check if text looks like a price."""
        if not text:
            return False
        
        # Remove whitespace
        text = text.strip()
        
        # Must contain digits
        if not any(c.isdigit() for c in text):
            return False
        
        # Common price indicators
        price_indicators = ['$', '€', '£', '¥', 'USD', 'EUR', 'GBP', 'CAD', 'AUD']
        has_indicator = any(ind in text for ind in price_indicators)
        
        # Should be relatively short (prices aren't long paragraphs)
        is_short = len(text) < 50
        
        # Contains decimal point or comma (common in prices)
        has_decimal = '.' in text or ',' in text
        
        # Return True if it has price indicators or looks numeric enough
        return has_indicator or (is_short and has_decimal)
    
    def get_detection_info(self) -> dict:
        """Get information about the last detection attempt."""
        return {
            'selector': self.last_detected_selector,
            'platform': self.last_detected_platform,
            'confidence': self.last_confidence,
        }
