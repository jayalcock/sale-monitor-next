import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup


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

    def extract_price(self, url: str, selector: str) -> Optional[float]:
        """Extract price from a webpage using CSS selector."""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code != 200:
                    logging.warning("GET %s -> %s", url, resp.status_code)
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                soup = BeautifulSoup(resp.text, "html.parser")
                el = soup.select_one(selector)
                if not el:
                    logging.warning("Selector not found: %s on %s", selector, url)
                    return None
                text = el.get_text(strip=True)
                price = self._parse_price(text)
                if price is not None:
                    return price
                logging.warning("Failed to parse price from: %s", text)
            except requests.exceptions.RequestException as e:
                logging.error("Request failed (attempt %d/%d): %s", attempt + 1, self.max_retries, e)

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff

        return None

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