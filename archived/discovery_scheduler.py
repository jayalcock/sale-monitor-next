"""
Background product discovery scheduler.

Periodically searches for monitored products at other retailers
and caches the results for dashboard display.
"""

import logging
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set
from threading import Thread, Event

from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state
from sale_monitor.services.product_discovery import ProductDiscovery


class DiscoveryScheduler:
    """Background scheduler for product discovery."""
    
    def __init__(
        self,
        products_csv: str,
        state_file: str,
        cache_file: str = "data/discovery_cache.json",
        interval_hours: int = 24,
        user_agent: str = "Mozilla/5.0 (compatible; SaleMonitor/1.0)",
        timeout: int = 10
    ):
        self.products_csv = products_csv
        self.state_file = state_file
        self.cache_file = cache_file
        self.interval_hours = interval_hours
        self.user_agent = user_agent
        self.timeout = timeout
        
        self._stop_event = Event()
        self._thread = None
    
    def _load_cache(self) -> Dict:
        """Load cached discovery results."""
        try:
            if Path(self.cache_file).exists():
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load discovery cache: {e}")
        return {}
    
    def _save_cache(self, cache: Dict):
        """Save discovery results to cache."""
        try:
            Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w') as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save discovery cache: {e}")
    
    def discover_all(self) -> List[Dict]:
        """
        Discover alternatives for all monitored products.
        
        Returns:
            List of suggestions in same format as /api/discovery/suggest
        """
        try:
            products = read_products(self.products_csv)
            state = load_state(self.state_file)
            existing_urls = {p.url for p in products}
            
            discovery = ProductDiscovery(
                user_agent=self.user_agent,
                timeout=self.timeout
            )
            
            suggestions = []
            
            for product in products:
                # Skip disabled products
                if not product.enabled:
                    continue
                
                product_state = state.get(product.url, {})
                identifiers = product_state.get('identifiers', {})
                
                # Skip if no identifiers
                if not identifiers:
                    logging.debug(f"Skipping {product.name}: no identifiers")
                    continue
                
                logging.info(f"Discovering alternatives for: {product.name}")
                
                try:
                    discovered = discovery.discover_products(
                        product_name=product.name,
                        identifiers=identifiers,
                        existing_urls=existing_urls,
                        current_url=product.url
                    )
                    
                    if discovered:
                        suggestions.append({
                            'original_product': {
                                'name': product.name,
                                'url': product.url
                            },
                            'discovered': discovered[:5],  # Top 5 matches
                            'count': len(discovered),
                            'last_updated': datetime.now(timezone.utc).isoformat()
                        })
                        logging.info(f"Found {len(discovered)} alternatives for {product.name}")
                    
                    # Rate limiting: wait between searches to be polite
                    time.sleep(2)
                    
                except Exception as e:
                    logging.error(f"Discovery failed for {product.name}: {e}")
                    continue
            
            return suggestions
            
        except Exception as e:
            logging.error(f"Discovery scan failed: {e}")
            return []
    
    def _discovery_loop(self):
        """Background discovery loop."""
        logging.info(f"Discovery scheduler started (interval: {self.interval_hours}h)")
        
        while not self._stop_event.is_set():
            try:
                # Run discovery
                logging.info("Starting product discovery scan...")
                suggestions = self.discover_all()
                
                # Update cache
                cache = {
                    'suggestions': suggestions,
                    'last_scan': datetime.now(timezone.utc).isoformat(),
                    'next_scan': datetime.now(timezone.utc).replace(
                        hour=(datetime.now(timezone.utc).hour + self.interval_hours) % 24
                    ).isoformat()
                }
                self._save_cache(cache)
                
                logging.info(f"Discovery scan complete: {len(suggestions)} products with alternatives")
                
            except Exception as e:
                logging.error(f"Discovery scan error: {e}")
            
            # Wait for next interval or stop signal
            self._stop_event.wait(self.interval_hours * 3600)
    
    def start(self):
        """Start background discovery scheduler."""
        if self._thread and self._thread.is_alive():
            logging.warning("Discovery scheduler already running")
            return
        
        self._stop_event.clear()
        self._thread = Thread(target=self._discovery_loop, daemon=True)
        self._thread.start()
        logging.info("Discovery scheduler thread started")
    
    def stop(self):
        """Stop background discovery scheduler."""
        if self._thread and self._thread.is_alive():
            logging.info("Stopping discovery scheduler...")
            self._stop_event.set()
            self._thread.join(timeout=5)
            logging.info("Discovery scheduler stopped")
    
    def get_cached_suggestions(self) -> List[Dict]:
        """Get cached discovery suggestions."""
        cache = self._load_cache()
        return cache.get('suggestions', [])
    
    def get_cache_info(self) -> Dict:
        """Get cache metadata."""
        cache = self._load_cache()
        return {
            'last_scan': cache.get('last_scan'),
            'next_scan': cache.get('next_scan'),
            'suggestion_count': len(cache.get('suggestions', []))
        }
