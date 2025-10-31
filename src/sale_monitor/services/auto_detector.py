"""
Auto-detection of price selectors for common e-commerce platforms.
"""
import re
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup


class PriceAutoDetector:
    """Attempts to automatically detect price elements on product pages."""
    
    # Common price element patterns (selector, confidence_weight)
    PATTERNS: List[Tuple[str, str, float]] = [
        # Shopify - more specific selectors first
        ('.price__sale .price-item--sale', 'shopify', 0.98),
        ('.price-item--sale', 'shopify', 0.95),
        ('.product-single__price .money', 'shopify', 0.95),
        ('.product__price .money', 'shopify', 0.95),
        ('[data-product-price]', 'shopify', 0.90),
        ('.price--main', 'shopify', 0.90),
        ('.product-price', 'shopify', 0.85),
        ('span.money', 'shopify', 0.80),
        
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
        ('.sale-price', 'generic', 0.70),
        ('.final-price', 'generic', 0.70),
        ('.current-price', 'generic', 0.75),
        ('.product-price', 'generic', 0.70),
        ('.price', 'generic', 0.60),
        ('#price', 'generic', 0.60),
    ]
    
    def __init__(self):
        self.last_detected_platform: Optional[str] = None
        self.last_detected_selector: Optional[str] = None
        self.last_confidence: Optional[float] = None
    
    def detect_price(self, html: str) -> Tuple[str, str, float]:
        """
        Try to automatically detect the price selector from HTML.
        
        Args:
            html: Raw HTML content of the product page
            
        Returns:
            Tuple of (selector, platform, confidence) if found, or ('', '', 0.0) if not found
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
                        if self._looks_like_price(text) and self._is_single_price(text):
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
        
        # Return empty tuple instead of None
        return ('', '', 0.0)
    
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
    
    @staticmethod
    def _is_single_price(text: str) -> bool:
        """Check if text contains only a single price (not multiple concatenated prices)."""
        if not text:
            return False
        
        # Count dollar signs - if more than 2, likely multiple prices
        dollar_count = text.count('$')
        if dollar_count > 2:
            return False
        
        # If exactly 2 dollar signs, check if they're part of duplicate prices
        if dollar_count == 2:
            # Split on dollar sign and check if we have duplicate amounts
            parts = text.split('$')
            if len(parts) == 3:  # Empty string before first $, then two price parts
                # Extract just the numeric parts of each price
                price1 = re.sub(r'[^\d.,]', '', parts[1])
                price2 = re.sub(r'[^\d.,]', '', parts[2])
                if price1 == price2:  # Duplicate prices like "$605.00$605.00"
                    return False
        
        # Count "price" keywords that might indicate multiple prices shown
        price_keywords = ['Regular price', 'Sale price', 'Unit price', 'from', 'SAVE', 'Save']
        keyword_count = sum(1 for keyword in price_keywords if keyword in text)
        if keyword_count >= 1:  # Changed from > 1 to >= 1
            return False
        
        # If text is too long (>80 chars), likely contains extra info
        if len(text) > 80:
            return False
        
        return True
    
    def get_detection_info(self) -> dict:
        """Get information about the last detection attempt."""
        return {
            'selector': self.last_detected_selector,
            'platform': self.last_detected_platform,
            'confidence': self.last_confidence,
        }
