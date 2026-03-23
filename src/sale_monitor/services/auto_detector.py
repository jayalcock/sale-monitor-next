"""
Auto-detection of price selectors for common e-commerce platforms.
"""
import re
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup


class PriceAutoDetector:
    """Attempts to automatically detect price elements on product pages."""
    
    # Common price element patterns (selector, confidence_weight)
    # Semantic / standards-based selectors first (stable across template changes),
    # then platform-specific selectors as fallbacks.
    PATTERNS: List[Tuple[str, str, float]] = [
        # --- Semantic / microdata / data-attribute selectors (most stable) ---
        ('[itemprop="price"]', 'generic', 0.92),
        ('[data-price]', 'generic', 0.88),
        ('[data-product-price]', 'generic', 0.88),
        ('[data-price-type="finalPrice"]', 'magento', 0.95),

        # --- Amazon ---
        ('.a-price.reinventPricePriceToPayMargin .a-offscreen', 'amazon', 0.98),
        ('.a-price[data-a-color="price"] .a-offscreen', 'amazon', 0.97),
        ('#corePriceDisplay_desktop_feature_div .a-price .a-offscreen', 'amazon', 0.96),
        ('.a-price-whole', 'amazon', 0.95),
        ('#priceblock_ourprice', 'amazon', 0.94),
        ('#priceblock_dealprice', 'amazon', 0.94),
        ('span.a-price.a-text-price.a-size-medium.apexPriceToPay .a-offscreen', 'amazon', 0.93),

        # --- Shopify ---
        ('.price.price--on-sale .price__regular', 'shopify', 0.98),
        ('.price__sale .price-item--sale', 'shopify', 0.98),
        ('.price-item--sale', 'shopify', 0.95),
        ('.product-single__price .money', 'shopify', 0.95),
        ('.product__price .money', 'shopify', 0.95),
        ('.price__regular .price-item--regular', 'shopify', 0.96),
        ('.price-item--regular', 'shopify', 0.95),
        ('.price__regular .price-item--price', 'shopify', 0.94),
        ('.price__regular', 'shopify', 0.92),
        ('.price--main', 'shopify', 0.90),
        ('.product-price', 'shopify', 0.85),
        ('span.money', 'shopify', 0.80),

        # --- WooCommerce ---
        ('.woocommerce-Price-amount.amount', 'woocommerce', 0.95),
        ('p.price ins .woocommerce-Price-amount', 'woocommerce', 0.95),
        ('p.price .amount', 'woocommerce', 0.90),
        ('.summary .price', 'woocommerce', 0.85),

        # --- BigCommerce ---
        ('.productView-price', 'bigcommerce', 0.95),

        # --- Magento ---
        ('.price-box .price', 'magento', 0.90),
        ('.product-info-price .price', 'magento', 0.85),

        # --- Generic fallbacks ---
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
    
    def detect_price_soup(self, soup: BeautifulSoup) -> Tuple[str, str, float]:
        """Detect price selector from an already-parsed BeautifulSoup.

        Avoids re-parsing HTML when the caller already has a soup object.
        """
        return self._detect_from_soup(soup)

    def detect_price(self, html: str) -> Tuple[str, str, float]:
        """
        Try to automatically detect the price selector from HTML.

        Args:
            html: Raw HTML content of the product page

        Returns:
            Tuple of (selector, platform, confidence) if found, or ('', '', 0.0) if not found
        """
        soup = BeautifulSoup(html, 'html.parser')
        return self._detect_from_soup(soup)

    def _detect_from_soup(self, soup: BeautifulSoup) -> Tuple[str, str, float]:
        """Shared detection logic operating on a parsed soup."""
        # Pass 1: strict price validation
        result = self._scan_patterns(soup, strict=True)
        if result:
            self.last_detected_selector, self.last_detected_platform, self.last_confidence = result
            return result

        # Pass 2: relaxed validation (any short text containing digits)
        import logging
        logging.getLogger(__name__).debug(
            "Auto-detect pass 1 found nothing; retrying with relaxed matching"
        )
        result = self._scan_patterns(soup, strict=False)
        if result:
            # Halve confidence to signal the match is weaker
            selector, platform, confidence = result
            confidence *= 0.5
            self.last_detected_selector = selector
            self.last_detected_platform = platform
            self.last_confidence = confidence
            return (selector, platform, confidence)

        return ('', '', 0.0)

    def _scan_patterns(
        self, soup: BeautifulSoup, *, strict: bool = True
    ) -> Optional[Tuple[str, str, float]]:
        """Scan PATTERNS against *soup*. When *strict* is False, accept any
        short numeric text even without a currency indicator."""
        best_match = None
        best_confidence = 0.0

        for selector, platform, confidence in self.PATTERNS:
            try:
                elements = soup.select(selector)
                if elements:
                    for elem in elements:
                        text = elem.get_text(strip=True)
                        ok = (
                            self._looks_like_price(text)
                            if strict
                            else self._looks_like_number(text)
                        )
                        if ok and self._is_single_price(text):
                            if confidence > best_confidence:
                                best_match = (selector, platform, confidence)
                                best_confidence = confidence
                            break
            except Exception:
                continue

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

    @staticmethod
    def _looks_like_number(text: str) -> bool:
        """Relaxed check: short text that contains at least one digit."""
        if not text:
            return False
        text = text.strip()
        return len(text) < 50 and any(c.isdigit() for c in text)
    
    @staticmethod
    def _is_single_price(text: str) -> bool:
        """Check if text contains only a single price (not multiple concatenated prices)."""
        if not text:
            return False

        # Strip leading visually-hidden labels that Shopify injects (Regular price / Sale price / Unit price)
        # so they don't trigger false multi-price rejection.
        text = re.sub(r'^(?:Regular price|Sale price|Unit price)\s*', '', text, flags=re.I)
        
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
        # Only reject when multiple distinct pricing/marketing keywords present (indicates composite block)
        if keyword_count >= 2:
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
