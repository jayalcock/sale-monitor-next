"""Shared price-check pipeline used by both the CLI and the web app.

One place for: extract price → choose currency → convert to base →
record history → merge the result into a state record.  Previously this
logic was duplicated (with drift) across the CLI cycle and three web
endpoints.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from sale_monitor.domain.models import Product
from sale_monitor.services.price_extractor import ExtractionResult, PriceExtractor
from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.utils import env_bool, utcnow_iso

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Outcome of one product check, ready to persist."""
    product: Product
    price: Optional[float] = None
    selector_source: str = ""
    currency: str = "CAD"
    currency_source: str = "default"
    base_currency: str = "CAD"
    price_in_base: Optional[float] = None
    identifiers: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.price is not None


class PriceCheckService:
    """Runs the full check pipeline for a product.

    ``check`` is safe to call from worker threads: it builds a fresh
    extractor per call (requests.Session is not thread-safe) and returns
    all results explicitly.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: int = 30,
        max_retries: int = 3,
        history=None,
        ex_service=None,
        config_file: str = "data/config.json",
        extractor_factory: Optional[Callable[[], PriceExtractor]] = None,
    ):
        self.history = history
        self.ex_service = ex_service
        self.config_file = config_file
        self._extractor_factory = extractor_factory or (
            lambda: PriceExtractor(
                user_agent=user_agent, timeout=timeout, max_retries=max_retries
            )
        )

    def new_extractor(self) -> PriceExtractor:
        """A fresh extractor configured like the ones used for checks."""
        return self._extractor_factory()

    # ── pipeline ─────────────────────────────────────────────────────────

    def check(self, product: Product, record_history: bool = True) -> CheckResult:
        """Extract price + currency for *product* and record it in history."""
        extractor = self._extractor_factory()
        extraction: ExtractionResult = extractor.extract(
            product.url, product.selector,
            default_currency=getattr(product, "currency", None) or "CAD",
        )

        base_currency = get_base_currency(self.config_file)
        result = CheckResult(product=product, base_currency=base_currency)

        if extraction.price is None:
            if record_history and self.history is not None:
                self.history.record_price(
                    product.url, product.name, None,
                    status="failed", currency=product.currency or "CAD",
                )
            return result

        currency, currency_source = self._choose_currency(product, extraction)
        result.price = extraction.price
        result.selector_source = extraction.selector_source
        result.currency = currency
        result.currency_source = currency_source
        result.identifiers = dict(extraction.identifiers or {})
        result.price_in_base = self._to_base(extraction.price, currency, base_currency)

        if record_history and self.history is not None:
            self.history.record_price(
                product.url, product.name, extraction.price,
                status="success", currency=currency,
                price_cad=result.price_in_base,
            )
        return result

    @staticmethod
    def _choose_currency(product: Product, extraction: ExtractionResult):
        """Detected currency wins by default; product config is the fallback."""
        detected = extraction.currency if extraction.currency_source != "default" else None
        prefer_detected = env_bool("PREFER_DETECTED_CURRENCY", True)
        if prefer_detected and detected:
            return detected, "detected"
        if getattr(product, "currency", None):
            return product.currency, "configured"
        if detected:
            return detected, "detected"
        return extraction.currency or "CAD", "default"

    def _to_base(self, price: float, currency: str, base_currency: str) -> Optional[float]:
        """Convert to base currency at the current rate (None if unavailable)."""
        if currency == base_currency:
            return price
        if self.ex_service is None:
            return None
        try:
            converted = self.ex_service.convert(float(price), currency, base_currency)
            return converted if converted is not None else None
        except (ValueError, TypeError):
            return None

    # ── persistence ──────────────────────────────────────────────────────

    @staticmethod
    def apply_to_state(
        state: Dict[str, Any],
        result: CheckResult,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Merge a successful check into a state dict (mutates *state*).

        Designed to run inside ``mutate_state`` so the merge happens against
        a freshly-read record: identifiers are merged key-wise and fields we
        don't set (e.g. ``group_key``, notification bookkeeping) survive.
        Returns the updated record.
        """
        p = result.product
        rec = state.get(p.url)
        if not isinstance(rec, dict):
            rec = {}
        prev_price = rec.get("current_price")

        merged_ids = dict(rec.get("identifiers") or {})
        merged_ids.update(result.identifiers or {})

        rec.update({
            "name": p.name,
            "url": p.url,
            "selector": p.selector,
            "selector_source": result.selector_source or rec.get("selector_source"),
            "current_price": result.price,
            "last_checked": utcnow_iso(),
            "last_price": prev_price if prev_price is not None else result.price,
            "currency": result.currency,
            "currency_source": result.currency_source,
            "price_in_base": result.price_in_base,
            "identifiers": merged_ids,
        })
        if extra_fields:
            rec.update(extra_fields)
        state[p.url] = rec
        return rec
