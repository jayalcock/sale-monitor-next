import logging
import re
import time
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from sale_monitor.services.auto_detector import PriceAutoDetector


class PriceExtractor:
    """Handles price extraction from web pages."""

    def __init__(self, user_agent: str, timeout: int = 30, max_retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
        })
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_detector = PriceAutoDetector()

    def extract_price(self, url: str, selector: str = "") -> Tuple[Optional[float], str]:
        """Extract price from a webpage using CSS selector or auto-detection.
        
        Returns:
            Tuple of (price, selector_source) where selector_source is 'manual', 'auto', or empty string on failure
        """
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code != 200:
                    logging.warning("GET %s -> %s", url, resp.status_code)
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                soup = BeautifulSoup(resp.text, "html.parser")
                
                # Try manual selector first if provided
                if selector:
                    el = soup.select_one(selector)
                    if el:
                        text = el.get_text(strip=True)
                        price = self._parse_price(text)
                        if price is not None:
                            return price, 'manual'
                        logging.warning("Failed to parse price from manual selector: %s", text)
                    else:
                        logging.warning("Manual selector not found: %s on %s", selector, url)
                
                # Try auto-detection if manual failed or no selector provided
                detected_selector, platform, confidence = self.auto_detector.detect_price(resp.text)
                if detected_selector:
                    el = soup.select_one(detected_selector)
                    if el:
                        text = el.get_text(strip=True)
                        price = self._parse_price(text)
                        if price is not None:
                            logging.info("Auto-detected price on %s using %s selector (confidence: %.0f%%)", 
                                       url, platform, confidence * 100)
                            return price, 'auto'
                        logging.warning("Auto-detected selector found but failed to parse price: %s", text)
                    else:
                        logging.warning("Auto-detected selector not found in soup: %s", detected_selector)
                
                # Both methods failed
                logging.warning("Failed to extract price from %s (selector: %s, auto-detection: %s)", 
                              url, selector or "none", "failed" if not detected_selector else "parse failed")
                
            except requests.exceptions.RequestException as e:
                logging.error("Request failed (attempt %d/%d): %s", attempt + 1, self.max_retries, e)

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff

        return None, ""

    def _parse_price(self, price_text: str) -> Optional[float]:
        """Parse numeric price from text, handling common separators."""
        if not price_text:
            return None

        # Keep only digits and separators
        s = re.sub(r"[^\d.,]", "", price_text)

        if not s:
            return None

        # If both separators exist, assume comma is thousands and dot is decimal (e.g., 1,234.56)
        if "," in s and "." in s:
            s = s.replace(",", "")
            try:
                return float(s)
            except ValueError:
                return None
        # If only comma exists, treat comma as decimal (e.g., 19,99 -> 19.99)
        if "," in s and "." not in s:
            s = s.replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return None

        # Plain float with dot or integer
        try:
            return float(s)
        except ValueError:
            return None