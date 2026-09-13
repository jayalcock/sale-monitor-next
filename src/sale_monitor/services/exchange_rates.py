"""
Exchange rate service with free API and caching.
"""
import logging
import time as _time
from typing import Dict, Iterable, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class ExchangeRateService:
    """Manages exchange rates with free API and fallback caching."""

    # Free API, no key required — v6 endpoint provides higher precision rates
    API_URL = "https://open.er-api.com/v6/latest/{base}"

    # Memory cache TTL: rates are valid for 1 hour
    _CACHE_TTL_SECONDS = 3600

    # Persistent cache TTL: older than this and we prefer an API refresh
    _DB_TTL_HOURS = 24

    # Fallback window when the API is down: serve the last known rate rather
    # than dropping conversions entirely. Staleness is surfaced separately
    # (health endpoint / dashboard) — this window must not hide outages.
    _FALLBACK_MAX_AGE_HOURS = 24 * 365

    def __init__(self, cache_handler=None, timeout: int = 10):
        """
        Initialize exchange rate service.

        Args:
            cache_handler: Object with cache_exchange_rate() and get_cached_rate()
                methods; a cache_exchange_rates() bulk method is used when present
            timeout: HTTP request timeout in seconds
        """
        self.cache_handler = cache_handler
        self.timeout = timeout
        # {from_currency: (timestamp, {to_currency: rate})}
        self._memory_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}

    def get_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        """
        Get exchange rate from from_currency to to_currency.

        Returns None if unable to get rate from API or cache.
        """
        # Same currency
        if from_currency == to_currency:
            return 1.0

        # Check memory cache first (with TTL)
        if from_currency in self._memory_cache:
            cached_time, cached_rates = self._memory_cache[from_currency]
            if _time.monotonic() - cached_time < self._CACHE_TTL_SECONDS:
                if to_currency in cached_rates:
                    return cached_rates[to_currency]
            else:
                del self._memory_cache[from_currency]

        # Check persistent cache
        cached_rate = self._cached_rate(from_currency, to_currency, self._DB_TTL_HOURS)
        if cached_rate is not None:
            logger.debug("Using cached rate: %s/%s = %s", from_currency, to_currency, cached_rate)
            # Update memory cache with current timestamp
            if from_currency in self._memory_cache:
                _, existing_rates = self._memory_cache[from_currency]
                existing_rates[to_currency] = cached_rate
            else:
                self._memory_cache[from_currency] = (_time.monotonic(), {to_currency: cached_rate})
            return cached_rate

        # Fetch from API
        rates = self.fetch_rates(from_currency)
        if rates is not None:
            if to_currency in rates:
                return rates[to_currency]
            logger.warning("Currency %s not found in API response for base %s",
                           to_currency, from_currency)

        # Fallback to last known cached rate (even if stale). Covers both a
        # failed fetch and a fetch that no longer includes to_currency.
        stale_rate = self._cached_rate(
            from_currency, to_currency, self._FALLBACK_MAX_AGE_HOURS
        )
        if stale_rate is not None:
            logger.warning("Using stale cached rate as fallback: %s/%s = %s",
                           from_currency, to_currency, stale_rate)
            return stale_rate

        return None

    def fetch_rates(self, base_currency: str) -> Optional[Dict[str, float]]:
        """Fetch fresh rates for *base_currency* from the API, persist them,
        and warm the memory cache.

        Returns the rates dict, or None if the fetch failed — never a stale
        fallback, so callers (e.g. the manual-refresh endpoint) can report
        failure honestly.
        """
        url = self.API_URL.format(base=base_currency)
        logger.debug("Fetching exchange rates from %s", url)
        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            rate_dict = {str(cur): float(value)
                         for cur, value in (data.get('rates') or {}).items()}
        except requests.exceptions.RequestException as e:
            logger.error("Failed to fetch exchange rates for %s: %s", base_currency, e)
            return None
        except (ValueError, KeyError, TypeError) as e:
            logger.error("Invalid exchange-rate API response for %s: %s", base_currency, e)
            return None
        if not rate_dict:
            logger.error("Exchange-rate API returned no rates for %s", base_currency)
            return None

        logger.info("Fetched %d rates for base %s", len(rate_dict), base_currency)
        self._persist_rates(base_currency, rate_dict)
        self._memory_cache[base_currency] = (_time.monotonic(), rate_dict)
        return rate_dict

    def refresh(self, base_currencies: Iterable[str]) -> Dict[str, bool]:
        """Force-fetch fresh rates for each base currency.

        Returns {base: fetch succeeded} so callers can report real failures
        instead of being masked by the stale-cache fallback.
        """
        return {base: self.fetch_rates(base) is not None
                for base in sorted(set(base_currencies))}

    def _cached_rate(self, from_currency: str, to_currency: str,
                     max_age_hours: int) -> Optional[float]:
        if not self.cache_handler:
            return None
        try:
            return self.cache_handler.get_cached_rate(
                from_currency, to_currency, max_age_hours=max_age_hours
            )
        except Exception as e:  # noqa: BLE001 - a broken cache is a miss, not an outage
            logger.error("Exchange-rate cache read failed: %s", e)
            return None

    def _persist_rates(self, base_currency: str, rate_dict: Dict[str, float]) -> None:
        if not self.cache_handler:
            return
        try:
            bulk = getattr(self.cache_handler, 'cache_exchange_rates', None)
            if bulk is not None:
                bulk(base_currency, rate_dict)
            else:
                for currency, value in rate_dict.items():
                    self.cache_handler.cache_exchange_rate(base_currency, currency, value)
        except Exception as e:  # noqa: BLE001 - persistence failure must not break conversions
            logger.error("Exchange-rate cache write failed: %s", e)

    def prefetch(self, from_currencies, to_currency: str):
        """Pre-warm the memory cache for a set of source currencies.

        Only makes API calls for currencies not already cached.  After this
        call, individual ``get_rate`` / ``convert`` calls for the same pairs
        will be served from memory.
        """
        for cur in set(from_currencies):
            if cur == to_currency:
                continue
            # Already warm?
            if cur in self._memory_cache:
                cached_time, cached_rates = self._memory_cache[cur]
                if _time.monotonic() - cached_time < self._CACHE_TTL_SECONDS and to_currency in cached_rates:
                    continue
            # This populates the memory cache (API or persistent fallback)
            self.get_rate(cur, to_currency)

    def clear_cache(self):
        """Clear the in-memory rate cache."""
        self._memory_cache.clear()

    def convert(self, amount: float, from_currency: str, to_currency: str) -> Optional[float]:
        """
        Convert amount from from_currency to to_currency.

        Returns None if exchange rate is unavailable.
        """
        rate = self.get_rate(from_currency, to_currency)
        if rate is None:
            return None
        return amount * rate
