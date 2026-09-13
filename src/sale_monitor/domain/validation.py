"""Shared validation/normalization for product input.

Single source of truth for every path that creates or modifies products
(web add, web update, web bulk import, CSV import).  Previously each
path hand-rolled its own parsing and they disagreed on what they
accepted — e.g. bulk import allowed negative prices the single-add
endpoint rejected, and CSV import validated nothing at all.
"""
import math
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

VALID_ALERT_RULES = frozenset({"target", "discount", "any_change", "price_drop", "below_avg"})
VALID_CHANNELS = frozenset({"smtp", "discord", "slack"})
MAX_NAME_LENGTH = 500
MAX_COOLDOWN_HOURS = 8760  # one year


class ProductValidationError(ValueError):
    """A product payload failed validation. str() is safe to show to users."""


def normalize_url(value, field_name: str = "url") -> str:
    """Validate and canonicalize a product URL.

    URL is the product's primary key (state.json, price history, and the
    products table all key on it), so cosmetic variants of the same URL
    must not slip past the duplicate check: whitespace is stripped and the
    fragment dropped (it never reaches the server).  Query strings are
    kept — the UI offers explicit tracking-parameter removal instead.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProductValidationError(f"{field_name} is required")
    url = value.strip()
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ProductValidationError(f"{field_name} is not a valid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise ProductValidationError(f"{field_name} must use http or https")
    if not parsed.hostname:
        raise ProductValidationError(f"{field_name} must include a host")
    return urlunparse(parsed._replace(fragment=""))


def validate_name(value) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise ProductValidationError("name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise ProductValidationError(f"name must be {MAX_NAME_LENGTH} characters or fewer")
    return name


def parse_price(value, field_name: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductValidationError(f"{field_name} must be a valid number") from exc
    if not math.isfinite(price):
        raise ProductValidationError(f"{field_name} must be a valid number")
    if price < 0:
        raise ProductValidationError(f"{field_name} must not be negative")
    return price


def parse_cooldown(value, default: int = 24) -> int:
    if value in (None, ""):
        return default
    try:
        hours = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductValidationError(
            "notification_cooldown_hours must be a valid positive integer"
        ) from exc
    if hours < 0:
        raise ProductValidationError("notification_cooldown_hours must not be negative")
    if hours > MAX_COOLDOWN_HOURS:
        raise ProductValidationError(
            f"notification_cooldown_hours must be {MAX_COOLDOWN_HOURS} or fewer"
        )
    return hours


def parse_currency(value, default: str = "CAD") -> str:
    if value in (None, ""):
        return default
    code = str(value).strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ProductValidationError("currency must be a 3-letter code like CAD or USD")
    return code


def parse_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "n", "off", "")
    return bool(value)


def parse_str_list(value, current: Optional[List[str]] = None) -> List[str]:
    """Comma-separated string or list → list of trimmed, non-empty strings.

    None means "not provided" (keep *current*); empty string/list clears.
    """
    if value is None:
        return list(current) if current is not None else []
    if not value:
        return []
    if isinstance(value, list):
        return [s.strip() for s in value if isinstance(s, str) and s.strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def parse_choices(value, valid: frozenset, field_name: str,
                  current: Optional[List[str]] = None) -> List[str]:
    """Like parse_str_list, but every entry must come from *valid*.

    Unknown values are rejected rather than stored: a typo like
    "pricedrop" would otherwise sit in the DB and silently never fire.
    """
    items = parse_str_list(value, current)
    unknown = [s for s in items if s not in valid]
    if unknown:
        raise ProductValidationError(
            f"{field_name} contains unknown values: {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(valid))})"
        )
    return items


def validate_product_payload(data: dict, *, current=None) -> dict:
    """Validate a JSON product payload into a dict of Product field values.

    With ``current=None`` (add): name and url are required, missing
    optional fields get defaults.  With ``current`` set (update): fields
    absent from the payload keep the current product's value, and the
    returned dict omits ``url`` (it is the lookup key, never updated).

    Raises ProductValidationError with a user-safe message.
    """
    is_update = current is not None

    def cur(attr, default=None):
        return getattr(current, attr, default) if is_update else default

    fields: Dict[str, Any] = {}
    if not is_update:
        fields["url"] = normalize_url(data.get("url"))

    if "name" in data or not is_update:
        fields["name"] = validate_name(data.get("name"))
    else:
        fields["name"] = current.name

    tp = data.get("target_price")
    fields["target_price"] = (
        parse_price(tp, "target_price") if tp not in (None, "") else cur("target_price")
    )
    dt = data.get("discount_threshold")
    fields["discount_threshold"] = (
        parse_price(dt, "discount_threshold") if dt not in (None, "") else cur("discount_threshold")
    )
    fields["notification_cooldown_hours"] = parse_cooldown(
        data.get("notification_cooldown_hours"),
        default=cur("notification_cooldown_hours", 24) or 24,
    )
    fields["currency"] = parse_currency(
        data.get("currency"), default=cur("currency", "CAD") or "CAD"
    )
    fields["enabled"] = parse_bool(data.get("enabled"), default=cur("enabled", True))

    if "selector" in data:
        sel = data.get("selector")
        fields["selector"] = sel.strip() if isinstance(sel, str) else ""
    else:
        fields["selector"] = cur("selector", "") or ""

    if "group" in data:
        grp = data.get("group")
        fields["group"] = (grp.strip() or None) if isinstance(grp, str) else None
    else:
        fields["group"] = cur("group")

    if "scrape_url" in data:
        raw = data.get("scrape_url")
        raw = raw.strip() if isinstance(raw, str) else raw
        fields["scrape_url"] = normalize_url(raw, "scrape_url") if raw else None
    else:
        fields["scrape_url"] = cur("scrape_url")

    fields["tags"] = parse_str_list(data.get("tags"), current=cur("tags", []))
    fields["alert_rules"] = parse_choices(
        data.get("alert_rules"), VALID_ALERT_RULES, "alert_rules",
        current=cur("alert_rules", []),
    )
    fields["notification_channels"] = parse_choices(
        data.get("notification_channels"), VALID_CHANNELS, "notification_channels",
        current=cur("notification_channels", []),
    )
    return fields


def validate_product(product) -> None:
    """Validate (and normalize in place) an already-built Product.

    Used by the CSV import path, which constructs Products leniently
    before deciding row by row what to keep.
    """
    product.url = normalize_url(product.url)
    product.name = validate_name(product.name)
    if product.scrape_url:
        product.scrape_url = normalize_url(product.scrape_url, "scrape_url")
    product.target_price = parse_price(product.target_price, "target_price")
    product.discount_threshold = parse_price(product.discount_threshold, "discount_threshold")
    product.notification_cooldown_hours = parse_cooldown(product.notification_cooldown_hours)
    product.currency = parse_currency(product.currency)
    product.alert_rules = parse_choices(product.alert_rules, VALID_ALERT_RULES, "alert_rules")
    product.notification_channels = parse_choices(
        product.notification_channels, VALID_CHANNELS, "notification_channels"
    )
