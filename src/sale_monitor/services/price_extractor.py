import logging
import re
import time
import json
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
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
        self.timeout = timeout
        self.max_retries = max_retries
        self.auto_detector = PriceAutoDetector()

    def extract_price(self, url: str, selector: str = "") -> Tuple[Optional[float], str]:
        """Extract price from a webpage using CSS selector or auto-detection.
        
        Returns:
            Tuple of (price, selector_source) where selector_source is 'manual', 'auto', or empty string on failure
        """
        import random
        
        # Add randomized delay for Amazon to avoid rate limiting
        if 'amazon.' in url.lower():
            delay = random.uniform(1.0, 3.0)
            time.sleep(delay)
            # Update headers with more realistic browser fingerprint
            self.session.headers.update({
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            })
        
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code != 200:
                    logging.warning("GET %s -> %s", url, resp.status_code)
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                
                # Log response size for debugging Amazon blocks
                if 'amazon' in url.lower():
                    logging.info("Amazon response: %d bytes, content-type: %s", 
                               len(resp.text), resp.headers.get('content-type', 'unknown'))
                    # Amazon bot detection check
                    if len(resp.text) < 10000 or 'robot check' in resp.text.lower() or 'captcha' in resp.text.lower():
                        logging.warning("Amazon bot detection triggered - response too small or contains captcha")
                        # Don't retry immediately for Amazon blocks
                        if attempt < self.max_retries - 1:
                            time.sleep(random.uniform(5.0, 10.0))
                        continue
                
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
                logging.debug("Auto-detector returned: selector='%s', platform='%s', confidence=%.2f", 
                            detected_selector, platform, confidence)
                if detected_selector:
                    el = soup.select_one(detected_selector)
                    logging.debug("Soup select result for '%s': %s", detected_selector, 'found' if el else 'not found')
                    if el:
                        text = el.get_text(strip=True)
                        logging.debug("Element text: '%s'", text[:100])
                        price = self._parse_price(text)
                        logging.debug("Parsed price: %s", price)
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

                # Fallback: try JSON-LD structured data for price (e.g., many retailers incl. JensonUSA)
                price_jsonld = self._extract_price_from_jsonld(resp.text)
                if price_jsonld is not None:
                    logging.info("Extracted price from JSON-LD on %s", url)
                    return price_jsonld, 'auto'
                
            except requests.exceptions.RequestException as e:
                logging.error("Request failed (attempt %d/%d): %s", attempt + 1, self.max_retries, e)

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff

        return None, ""

    # --- Currency helpers (non-breaking additions) ---
    def extract_price_with_currency(self, url: str, selector: str = "", default_currency: str = "CAD") -> Tuple[Optional[float], str, str]:
        """Extract price and attempt to infer currency.

        Returns (price, selector_source, currency).
        Currency inference falls back to default_currency if not inferable.
        """
        price, source = self.extract_price(url, selector)

        detected_currency: Optional[str] = None
        heuristic_currency: Optional[str] = None
        host_guess_currency: Optional[str] = None
        self.last_currency_source = 'default'
        # Try to fetch HTML to detect currency from page content (JSON-LD/meta/Shopify)
        # Skip detection for known test/local hosts and when explicitly disabled via env.
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()

        import os as _os
        detection_disabled = _os.getenv('SALEMONITOR_DISABLE_HTML_CURRENCY', '0') == '1'
        skip_hosts = {'example.com', 'localhost', '127.0.0.1', '0.0.0.0'}

        if not detection_disabled and host not in skip_hosts:
            try:
                # Cap detection timeout to 5s to avoid long hangs
                detection_timeout = min(int(self.timeout), 5) if isinstance(self.timeout, int) else 5
                resp = self.session.get(url, timeout=detection_timeout)
                if resp.status_code == 200 and resp.text:
                    detected_currency = self._detect_currency_from_html(resp.text, url)
                    if not detected_currency:
                        # Heuristic body scan if structured tags missing
                        body_l = resp.text.lower()
                        has_ca = ('ca$' in body_l) or (' cad' in body_l)
                        has_us = ('us$' in body_l) or (' usd' in body_l)
                        if has_ca and not has_us:
                            heuristic_currency = 'CAD'
                        elif has_us and not has_ca:
                            heuristic_currency = 'USD'
            except requests.exceptions.RequestException:
                pass

        if not detected_currency:
            host_guess_currency = self._guess_currency_from_url(url)
        currency = detected_currency or heuristic_currency or host_guess_currency or default_currency

        if detected_currency:
            self.last_currency_source = 'html'
        elif heuristic_currency:
            self.last_currency_source = 'heuristic'
        elif host_guess_currency:
            self.last_currency_source = 'host'
        else:
            self.last_currency_source = 'default'

        return price, source, currency

    def _detect_currency_from_html(self, html: str, url: str) -> Optional[str]:
        """Detect currency code (CAD, USD, etc.) from HTML content.

        Methods:
        - JSON-LD Offers.priceCurrency
        - Meta tags (og:price:currency, product:price:currency, itemprop priceCurrency)
        - Shopify globals (ShopifyAnalytics.meta.currency, Shopify.currency.active)
        - data-currency attributes
        - Lightweight heuristics for CA$/USD tokens in body when tags are absent
        - .ca TLD fallback
        """
        if not html:
            return None

        # 1) JSON-LD blocks
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            block = m.group(1).strip()
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            c = self._find_price_currency_in_json(data)
            if c and re.fullmatch(r'[A-Z]{3}', c):
                return c

        # 2) Meta tags
        meta_patterns = (
            r'<meta[^>]+property=["\']og:price:currency["\'][^>]+content=["\']([A-Z]{3})["\']',
            r'<meta[^>]+property=["\']product:price:currency["\'][^>]+content=["\']([A-Z]{3})["\']',
            r'<meta[^>]+itemprop=["\']priceCurrency["\'][^>]+content=["\']([A-Z]{3})["\']',
            r'<meta[^>]+name=["\']currency["\'][^>]+content=["\']([A-Z]{3})["\']',
        )
        for pat in meta_patterns:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1).upper()

        # 3) Shopify hints
        m = re.search(r'ShopifyAnalytics\s*=\s*{.*?"currency"\s*:\s*"([A-Z]{3})"', html, re.I | re.S)
        if m:
            return m.group(1).upper()
        m = re.search(r'Shopify\s*\.\s*currency\s*\.\s*active\s*=\s*"([A-Z]{3})"', html, re.I)
        if m:
            return m.group(1).upper()
        m = re.search(r'data-(?:shop-)?currency=["\']([A-Z]{3})["\']', html, re.I)
        if m:
            return m.group(1).upper()

        # 3.5) Heuristic tokens in body text close enough to pricing
        # Prefer explicit country-prefixed symbols if only one family is present.
        try:
            body_l = html.lower()
            has_ca = ('ca$' in body_l) or (' c$' in body_l) or (' cad' in body_l)
            has_us = ('us$' in body_l) or (' usd' in body_l)
            if has_ca and not has_us:
                return 'CAD'
            if has_us and not has_ca:
                return 'USD'
        except Exception:
            pass

        # 4) .ca fallback
        if re.search(r'://[^/]+\.ca(?:/|$)', url, re.I):
            return 'CAD'

        return None

    def _find_price_currency_in_json(self, obj) -> Optional[str]:
        """Walk arbitrary JSON to find a priceCurrency field."""
        if isinstance(obj, dict):
            pc = obj.get('priceCurrency')
            if isinstance(pc, str):
                return pc.upper()
            # Common nesting under offers
            if 'offers' in obj:
                c = self._find_price_currency_in_json(obj['offers'])
                if c:
                    return c
            for v in obj.values():
                c = self._find_price_currency_in_json(v)
                if c:
                    return c
        elif isinstance(obj, list):
            for it in obj:
                c = self._find_price_currency_in_json(it)
                if c:
                    return c
        return None

    def _guess_currency_from_url(self, url: str) -> Optional[str]:
        """Best-effort currency guess from URL/host.

        Rules:
        - Known USD domains -> USD
        - .ca TLD -> CAD
        - Otherwise return None (let explicit product currency or default win)
        """
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""

        host_l = host.lower()
        # Known USD-only retailers
        usd_hosts = (
            "jensonusa.com", "jensenusa.com", "amazon.com", "competitivecyclist.com",
            "backcountry.com", "rei.com", "trekbikes.com", "specialized.com",
        )
        if any(host_l.endswith(h) for h in usd_hosts):
            return "USD"
        
        # Amazon country-specific domains
        if "amazon.ca" in host_l:
            return "CAD"
        elif "amazon.co.uk" in host_l:
            return "GBP"
        elif "amazon.de" in host_l or "amazon.fr" in host_l or "amazon.it" in host_l or "amazon.es" in host_l:
            return "EUR"
        elif "amazon.co.jp" in host_l:
            return "JPY"
        elif "amazon.com.au" in host_l:
            return "AUD"

        if host_l.endswith(".ca"):
            return "CAD"

        # Removed generic .com -> USD fallback to avoid mislabeling CAD-priced .com stores

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

    # --- JSON-LD price extraction helpers ---
    def _extract_price_from_jsonld(self, html: str) -> Optional[float]:
        """Extract price from application/ld+json blocks when present.

        Looks for Offer/Offers.price, AggregateOffer.lowPrice, priceSpecification.price,
        or generic 'price' fields. Returns the first reasonable positive price found.
        """
        if not html:
            return None
        prices = []
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            block = m.group(1).strip()
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            self._collect_prices_from_json(data, prices)
            if prices:
                # Prefer the smallest positive plausible price to avoid capturing strikethrough lists
                positives = [p for p in prices if isinstance(p, (int, float)) and p > 0]
                if positives:
                    return float(min(positives))
        return None

    def _collect_prices_from_json(self, obj, out_list):
        """Recursively collect numeric price candidates from JSON-LD structures."""
        try:
            if isinstance(obj, dict):
                # Common locations
                if 'price' in obj:
                    try:
                        out_list.append(float(obj['price']))
                    except (TypeError, ValueError):
                        pass
                if 'lowPrice' in obj:
                    try:
                        out_list.append(float(obj['lowPrice']))
                    except (TypeError, ValueError):
                        pass
                # Nested structures
                for key in ('offers', 'Offer', 'AggregateOffer', 'priceSpecification'):
                    if key in obj:
                        self._collect_prices_from_json(obj[key], out_list)
                for v in obj.values():
                    self._collect_prices_from_json(v, out_list)
            elif isinstance(obj, list):
                for it in obj:
                    self._collect_prices_from_json(it, out_list)
        except Exception:
            # Be resilient to malformed vendor JSON
            pass