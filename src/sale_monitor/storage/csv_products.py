import csv
from pathlib import Path
from typing import List, Optional

from sale_monitor.domain.models import Product


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


def read_products(csv_path: str) -> List[Product]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Products CSV not found at: {csv_path}")

    products: List[Product] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"name", "url", "selector"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            product = Product(
                name=row["name"].strip(),
                url=row["url"].strip(),
                selector=row["selector"].strip(),
                target_price=_parse_float(row.get("target_price")),
                discount_threshold=_parse_float(row.get("discount_threshold")),
                enabled=_parse_bool(row.get("enabled", "true")),
                notification_cooldown_hours=_parse_int(row.get("notification_cooldown_hours"), 24),
            )
            products.append(product)
    return products