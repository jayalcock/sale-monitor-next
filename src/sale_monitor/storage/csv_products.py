import csv
from pathlib import Path
from typing import List, Optional  # noqa: F811 (List used by _parse_list)

from sale_monitor.domain.models import Product

CSV_COLUMNS = [
    'name', 'url', 'target_price', 'discount_threshold', 'selector',
    'enabled', 'notification_cooldown_hours', 'selector_source', 'currency',
    'group', 'tags', 'alert_rules', 'notification_channels',
]


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() not in ("false", "0", "no", "n")


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_list(value: Optional[str]) -> List[str]:
    """Parse a comma-separated string into a list of trimmed, non-empty strings."""
    if value is None:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def read_products(csv_path: str) -> List[Product]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Products CSV not found at: {csv_path}")

    products: List[Product] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"name", "url"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            product = Product(
                name=row["name"].strip(),
                url=row["url"].strip(),
                selector=row.get("selector", "").strip(),
                target_price=_parse_float(row.get("target_price")),
                discount_threshold=_parse_float(row.get("discount_threshold")),
                enabled=_parse_bool(row.get("enabled", "true")),
                notification_cooldown_hours=_parse_int(row.get("notification_cooldown_hours"), 24),
                group=row.get("group", "").strip() or None,
                selector_source=row.get("selector_source", "").strip() or None,
                currency=row.get("currency", "CAD").strip() or "CAD",
                tags=_parse_list(row.get("tags")),
                alert_rules=_parse_list(row.get("alert_rules")),
                notification_channels=_parse_list(row.get("notification_channels")),
            )
            products.append(product)
    return products


def export_products_csv(filepath: str, products: List[Product]) -> None:
    """Write products to a CSV file (export format)."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for p in products:
            writer.writerow([
                p.name,
                p.url,
                p.target_price if p.target_price is not None else '',
                p.discount_threshold if p.discount_threshold is not None else '',
                p.selector,
                'true' if p.enabled else 'false',
                p.notification_cooldown_hours,
                p.selector_source if p.selector_source else '',
                p.currency if hasattr(p, 'currency') else 'CAD',
                p.group if getattr(p, 'group', None) else '',
                ','.join(getattr(p, 'tags', []) or []),
                ','.join(getattr(p, 'alert_rules', []) or []),
                ','.join(getattr(p, 'notification_channels', []) or []),
            ])