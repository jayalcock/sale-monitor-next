from dataclasses import dataclass
from typing import Optional


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

# Additional models can be defined here as needed for future expansion.