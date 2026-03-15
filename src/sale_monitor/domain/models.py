from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Product:
    """Represents a product to monitor."""
    name: str
    url: str
    selector: str
    target_price: Optional[float] = None
    discount_threshold: Optional[float] = None
    enabled: bool = True
    notification_cooldown_hours: int = 24
    current_price: Optional[float] = None
    selector_source: Optional[str] = None  # 'manual', 'auto', 'bookmarklet'
    currency: str = "CAD"  # Currency for target_price and discount calculations
    group: Optional[str] = None  # Explicit competitive group slug
    tags: List[str] = field(default_factory=list)
    alert_rules: List[str] = field(default_factory=list)  # e.g. ['target','discount','any_change','below_avg','price_drop']
    notification_channels: List[str] = field(default_factory=list)  # e.g. ['smtp','discord','slack'] — empty = use global defaults

# Additional models can be defined here as needed for future expansion.