"""
Product Discovery Service - Find same products at other retailers.

Searches for products using identifiers (MPN, SKU, GTIN) or normalized names
across configured retailer sites.
"""

import logging
import re
from typing import List, Dict, Optional, Set
from urllib.parse import urljoin, quote_plus
import requests
from bs4 import BeautifulSoup


class ProductDiscovery:
    """Find same products at other retailers."""
    
    # Common Canadian retailers with search URL patterns
    RETAILERS = {
        'amazon.ca': {
            'search_url': 'https://www.amazon.ca/s?k={query}',
            'selectors': {
                'results': 'div[data-component-type="s-search-result"]',
                'title': 'h2 a span',
                'link': 'h2 a',
                'price': 'span.a-price span.a-offscreen'
            }
        },
        'bestbuy.ca': {
            'search_url': 'https://www.bestbuy.ca/en-ca/search?search={query}',
            'selectors': {
                'results': 'div.productItemWrapper',
                'title': 'div.productItemName',
                'link': 'a.link',
                'price': 'span.screenReaderOnly'
            }
        },
        'canadiantire.ca': {
            'search_url': 'https://www.canadiantire.ca/en/search-results.html?q={query}',
            'selectors': {
                'results': 'div.nl-product',
                'title': 'a.nl-product__title',
                'link': 'a.nl-product__title',
                'price': 'span.nl-price__value'
            }
        },
        'walmart.ca': {
            'search_url': 'https://www.walmart.ca/search?q={query}',
            'selectors': {
                'results': 'div[data-testid="product-tile"]',
                'title': 'span[data-automation="product-title"]',
                'link': 'a',
                'price': 'div[data-automation="product-price"]'
            }
        },
        'jensonusa.com': {
            'search_url': 'https://www.jensonusa.com/search?query={query}',
            'selectors': {
                'results': 'div.product-tile',
                'title': 'a.product-name',
                'link': 'a.product-name',
                'price': 'span.price-sales'
            }
        },
        'competitivecyclist.com': {
            'search_url': 'https://www.competitivecyclist.com/Store/catalog/search.jsp?s={query}',
            'selectors': {
                'results': 'div.ui-pl-result',
                'title': 'a.ui-pl-name',
                'link': 'a.ui-pl-name',
                'price': 'span.ui-pl-pricing-price'
            }
        }
    }
    
    def __init__(self, user_agent: str = "Mozilla/5.0 (compatible; SaleMonitor/1.0)", timeout: int = 10):
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': user_agent})
    
    def normalize_name(self, name: str) -> str:
        """Normalize product name for comparison."""
        if not name:
            return ""
        # Lowercase, remove extra spaces, special chars
        normalized = name.lower().strip()
        normalized = re.sub(r'[^\w\s-]', ' ', normalized)
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized
    
    def build_search_query(self, identifiers: Dict[str, str], product_name: str) -> str:
        """Build search query from identifiers or product name."""
        # Prefer MPN/SKU/GTIN for precise search
        if identifiers.get('mpn'):
            return identifiers['mpn']
        if identifiers.get('sku'):
            return identifiers['sku']
        if identifiers.get('gtin') or identifiers.get('gtin13'):
            return identifiers.get('gtin') or identifiers.get('gtin13')
        
        # Fallback to brand + model or normalized name
        if identifiers.get('brand') and identifiers.get('model'):
            return f"{identifiers['brand']} {identifiers['model']}"
        
        # Use first 5-6 meaningful words from name
        words = self.normalize_name(product_name).split()[:6]
        return ' '.join(words)
    
    def search_retailer(self, retailer_domain: str, query: str, existing_urls: Set[str]) -> List[Dict]:
        """Search a specific retailer for products matching query."""
        config = self.RETAILERS.get(retailer_domain)
        if not config:
            return []
        
        search_url = config['search_url'].format(query=quote_plus(query))
        results = []
        
        try:
            resp = self.session.get(search_url, timeout=self.timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')
            
            # Find result items
            items = soup.select(config['selectors']['results'])
            
            for item in items[:5]:  # Limit to top 5 results per retailer
                try:
                    # Extract title
                    title_el = item.select_one(config['selectors']['title'])
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    
                    # Extract link
                    link_el = item.select_one(config['selectors']['link'])
                    if not link_el:
                        continue
                    href = link_el.get('href', '')
                    if not href:
                        continue
                    
                    # Make absolute URL
                    if not href.startswith('http'):
                        base_url = f"https://{retailer_domain}"
                        product_url = urljoin(base_url, href)
                    else:
                        product_url = href
                    
                    # Skip if already monitoring this URL
                    if product_url in existing_urls:
                        continue
                    
                    # Extract price (optional, may not always be available)
                    price_text = None
                    price_el = item.select_one(config['selectors']['price'])
                    if price_el:
                        price_text = price_el.get_text(strip=True)
                    
                    results.append({
                        'name': title,
                        'url': product_url,
                        'retailer': retailer_domain,
                        'price_text': price_text,
                        'search_query': query
                    })
                    
                except Exception as e:
                    logging.debug(f"Error parsing result from {retailer_domain}: {e}")
                    continue
            
        except Exception as e:
            logging.warning(f"Failed to search {retailer_domain}: {e}")
        
        return results
    
    def discover_products(
        self,
        product_name: str,
        identifiers: Dict[str, str],
        existing_urls: Set[str],
        current_url: str
    ) -> List[Dict]:
        """
        Discover same product at other retailers.
        
        Args:
            product_name: Name of the product
            identifiers: Dict of product identifiers (mpn, sku, gtin, etc.)
            existing_urls: Set of URLs already being monitored
            current_url: URL of the current product (to skip same retailer)
        
        Returns:
            List of discovered products with name, url, retailer
        """
        # Build search query
        query = self.build_search_query(identifiers, product_name)
        if not query:
            return []
        
        # Extract current retailer domain to potentially skip it
        current_domain = None
        try:
            from urllib.parse import urlparse
            parsed = urlparse(current_url)
            current_domain = parsed.netloc.replace('www.', '')
        except:
            pass
        
        # Search all configured retailers
        all_results = []
        for retailer_domain in self.RETAILERS.keys():
            # Optionally skip current retailer
            # (commented out to allow finding different products at same retailer)
            # if current_domain and retailer_domain in current_domain:
            #     continue
            
            results = self.search_retailer(retailer_domain, query, existing_urls)
            all_results.extend(results)
        
        return all_results
