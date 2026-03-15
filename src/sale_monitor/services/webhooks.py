"""Webhook notification services for Discord and Slack."""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional

import requests

logger = logging.getLogger(__name__)


class WebhookNotifier:
    """Base class for webhook notifiers."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url

    def send(
        self,
        product_name: str,
        product_url: str,
        current_price: float,
        currency: str = "CAD",
        price_in_base: Optional[float] = None,
        base_currency: str = "CAD",
        old_price: Optional[float] = None,
        target_price: Optional[float] = None,
        triggered_by: str = "target",
    ) -> bool:
        raise NotImplementedError

    def send_test(self) -> bool:
        """Send a test message to verify the webhook is configured correctly."""
        return self.send(
            product_name="Test Product",
            product_url="https://example.com/product",
            current_price=29.99,
            currency="CAD",
            old_price=39.99,
            triggered_by="test",
        )


class DiscordWebhookNotifier(WebhookNotifier):
    """Send notifications to a Discord channel via webhook."""

    def send(
        self,
        product_name: str,
        product_url: str,
        current_price: float,
        currency: str = "CAD",
        price_in_base: Optional[float] = None,
        base_currency: str = "CAD",
        old_price: Optional[float] = None,
        target_price: Optional[float] = None,
        triggered_by: str = "target",
    ) -> bool:
        fields: List[Dict[str, Any]] = [
            {"name": "Price", "value": f"${current_price:.2f} {currency}", "inline": True},
        ]
        if price_in_base is not None and currency != base_currency:
            fields.append({"name": f"~{base_currency}", "value": f"${price_in_base:.2f}", "inline": True})
        if old_price is not None:
            delta = current_price - old_price
            sign = "-" if delta < 0 else "+"
            fields.append({"name": "Change", "value": f"{sign}${abs(delta):.2f}", "inline": True})
        if target_price is not None:
            fields.append({"name": "Target", "value": f"${target_price:.2f}", "inline": True})
        fields.append({"name": "Trigger", "value": triggered_by, "inline": True})

        color = 0x2D6A4F if (old_price and current_price < old_price) else 0x6C757D
        payload = {
            "embeds": [{
                "title": f"Sale Alert: {product_name}",
                "url": product_url,
                "color": color,
                "fields": fields,
            }],
        }
        return self._post(payload)

    def _post(self, payload: Dict[str, Any]) -> bool:
        try:
            resp = requests.post(self.url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                return True
            logger.warning("Discord webhook %s returned %d: %s", self.name, resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as e:
            logger.error("Discord webhook %s failed: %s", self.name, e)
            return False


class SlackWebhookNotifier(WebhookNotifier):
    """Send notifications to a Slack channel via incoming webhook."""

    def send(
        self,
        product_name: str,
        product_url: str,
        current_price: float,
        currency: str = "CAD",
        price_in_base: Optional[float] = None,
        base_currency: str = "CAD",
        old_price: Optional[float] = None,
        target_price: Optional[float] = None,
        triggered_by: str = "target",
    ) -> bool:
        parts = [f"*<{product_url}|{product_name}>*", f"Price: ${current_price:.2f} {currency}"]
        if price_in_base is not None and currency != base_currency:
            parts.append(f"~${price_in_base:.2f} {base_currency}")
        if old_price is not None:
            delta = current_price - old_price
            sign = "-" if delta < 0 else "+"
            parts.append(f"Change: {sign}${abs(delta):.2f}")
        if target_price is not None:
            parts.append(f"Target: ${target_price:.2f}")
        parts.append(f"Trigger: {triggered_by}")

        payload = {"text": "\n".join(parts)}
        return self._post(payload)

    def _post(self, payload: Dict[str, Any]) -> bool:
        try:
            resp = requests.post(self.url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            logger.warning("Slack webhook %s returned %d: %s", self.name, resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as e:
            logger.error("Slack webhook %s failed: %s", self.name, e)
            return False


def build_notifiers_from_config(webhooks: List[Dict[str, Any]]) -> List[WebhookNotifier]:
    """Build a list of notifier instances from webhook config entries."""
    notifiers: List[WebhookNotifier] = []
    for wh in webhooks:
        if not wh.get("enabled") or not wh.get("url"):
            continue
        wh_type = wh.get("type", "").lower()
        name = wh.get("name", wh_type)
        url = wh["url"]
        if wh_type == "discord":
            notifiers.append(DiscordWebhookNotifier(name=name, url=url))
        elif wh_type == "slack":
            notifiers.append(SlackWebhookNotifier(name=name, url=url))
        else:
            logger.warning("Unknown webhook type: %s", wh_type)
    return notifiers
