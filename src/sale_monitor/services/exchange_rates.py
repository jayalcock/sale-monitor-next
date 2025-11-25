"""
Exchange rate service with free API and caching.
"""
import logging
import requests
from typing import Optional, Dict


logger = logging.getLogger(__name__)


class ExchangeRateService:
    """Manages exchange rates with free API and fallback caching."""
    
    # Free API, no key required
    API_URL = "https://api.exchangerate-api.com/v4/latest/{base}"
    
    def __init__(self, cache_handler=None, timeout: int = 10):
        """
        Initialize exchange rate service.
        
        Args:
            cache_handler: Object with cache_exchange_rate() and get_cached_rate() methods
            timeout: HTTP request timeout in seconds
        """
        self.cache_handler = cache_handler
        self.timeout = timeout
        self._memory_cache: Dict[str, Dict[str, float]] = {}
    
    def get_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        """
        Get exchange rate from from_currency to to_currency.
        
        Returns None if unable to get rate from API or cache.
        """
        # Same currency
        if from_currency == to_currency:
            return 1.0
        
        # Check memory cache first (for current session)
        if from_currency in self._memory_cache:
            if to_currency in self._memory_cache[from_currency]:
                return self._memory_cache[from_currency][to_currency]
        
        # Check persistent cache
        if self.cache_handler:
            cached_rate = self.cache_handler.get_cached_rate(from_currency, to_currency)
            if cached_rate is not None:
                logger.debug("Using cached rate: %s/%s = %s", from_currency, to_currency, cached_rate)
                # Update memory cache
                if from_currency not in self._memory_cache:
                    self._memory_cache[from_currency] = {}
                self._memory_cache[from_currency][to_currency] = cached_rate
                return cached_rate
        
        # Fetch from API
        try:
            url = self.API_URL.format(base=from_currency)
            logger.debug("Fetching exchange rates from %s", url)
            
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            rates = data.get('rates', {})
            
            if to_currency not in rates:
                logger.warning("Currency %s not found in API response", to_currency)
                return None
            
            rate = float(rates[to_currency])
            logger.info("Fetched rate: %s/%s = %s", from_currency, to_currency, rate)
            
            # Cache the entire rates dict
            if from_currency not in self._memory_cache:
                self._memory_cache[from_currency] = {}
            
            for currency, value in rates.items():
                self._memory_cache[from_currency][currency] = float(value)
                # Persist to cache
                if self.cache_handler:
                    self.cache_handler.cache_exchange_rate(from_currency, currency, float(value))
            
            return rate
            
        except requests.exceptions.RequestException as e:
            logger.error("Failed to fetch exchange rate: %s", e)
            
            # Fallback to last known cached rate (even if stale)
            if self.cache_handler:
                # Try to get rate with no age limit as fallback
                with_no_limit = self.cache_handler.get_cached_rate(
                    from_currency, to_currency, max_age_hours=24*365  # 1 year
                )
                if with_no_limit is not None:
                    logger.warning("Using stale cached rate as fallback: %s/%s = %s",
                                   from_currency, to_currency, with_no_limit)
                    return with_no_limit
            
            return None
        except (ValueError, KeyError) as e:
            logger.error("Invalid API response: %s", e)
            return None
    
    def convert(self, amount: float, from_currency: str, to_currency: str) -> Optional[float]:
        """
        Convert amount from from_currency to to_currency.
        
        Returns None if exchange rate is unavailable.
        """
        rate = self.get_rate(from_currency, to_currency)
        if rate is None:
            return None
        return amount * rate
