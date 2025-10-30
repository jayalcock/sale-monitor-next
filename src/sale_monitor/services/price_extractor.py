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
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        self.timeout = timeout
        self.max_retries = max_retries
    
    def extract_price(self, url: str, selector: str) -> Optional[float]:
        """Extract price from a webpage using CSS selector."""
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                
                soup = BeautifulSoup(response.content, 'html.parser')
                price_element = soup.select_one(selector)
                
                if not price_element:
                    logging.warning(f"Price element not found with selector: {selector}")
                    return None
                
                price_text = price_element.get_text(strip=True)
                return self._parse_price(price_text)
                
            except requests.exceptions.RequestException as e:
                logging.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            
            if attempt == self.max_retries - 1:
                logging.error(f"All attempts failed for {url}")
                return None
            time.sleep(2 ** attempt)  # Exponential backoff
        
        return None
    
    def _parse_price(self, price_text: str) -> Optional[float]:
        """Parse price from text string."""
        # Remove common currency symbols and whitespace
        price_text = re.sub(r'[^\d.,]', '', price_text)
        
        # Handle different decimal separators
        if ',' in price_text and '.' in price_text:
            # Assume comma is thousands separator
            price_text = price_text.replace(',', '')
        elif ',' in price_text:
            # Could be decimal separator in some locales
            if price_text.count(',') == 1 and len(price_text.split(',')[1]) <= 2:
                price_text = price_text.replace(',', '.')
            else:
                price_text = price_text.replace(',', '')
        
        try:
            return float(price_text)
        except ValueError:
            logging.error(f"Could not parse price from: {price_text}")
            return None