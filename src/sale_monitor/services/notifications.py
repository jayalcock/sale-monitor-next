import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SmtpConfig:
    server: str
    port: int
    username: str
    password: str
    from_email: str
    to_email: str
    enable: bool = True
    use_starttls: bool = True


class NotificationManager:
    MAX_RETRIES = 3
    BACKOFF_BASE = 2  # seconds

    def __init__(self, config: SmtpConfig):
        self.config = config

    def send_sale_notification(
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
    ) -> None:
        if not self.config.enable:
            return

        # Subject shows base currency conversion when currency differs from base
        subject_suffix = ""
        if price_in_base is not None and currency != base_currency:
            subject_suffix = f" (${price_in_base:.2f} {base_currency})"
        subject = f"Sale Monitor: {product_name} at ${current_price:.2f} {currency}{subject_suffix}"

        # Build plain-text body
        lines = [
            f"Product: {product_name}",
            f"URL: {product_url}",
            f"Current Price: ${current_price:.2f} {currency}",
        ]
        if price_in_base is not None and currency != base_currency:
            lines.append(f"Approx {base_currency}: ${price_in_base:.2f} {base_currency}")
        if old_price is not None:
            lines.append(f"Previous Price: ${old_price:.2f}")
            delta = current_price - old_price
            lines.append(f"Change: {'-' if delta < 0 else '+'}${abs(delta):.2f}")
        if target_price is not None:
            lines.append(f"Target Price: ${target_price:.2f} {currency}")
        lines.append(f"Trigger: {triggered_by}")
        text_body = "\n".join(lines)

        # Build HTML body
        html_body = self._build_html(
            product_name=product_name,
            product_url=product_url,
            current_price=current_price,
            currency=currency,
            price_in_base=price_in_base,
            base_currency=base_currency,
            old_price=old_price,
            target_price=target_price,
            triggered_by=triggered_by,
        )

        msg = MIMEMultipart("alternative")
        msg["From"] = self.config.from_email
        msg["To"] = self.config.to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        self._send_with_retry(msg)

    def _send_with_retry(self, msg: MIMEMultipart) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                self._send_smtp(msg)
                return
            except (smtplib.SMTPException, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    delay = self.BACKOFF_BASE ** attempt
                    logger.warning(
                        "SMTP attempt %d/%d failed: %s — retrying in %ds",
                        attempt, self.MAX_RETRIES, exc, delay,
                    )
                    time.sleep(delay)
        logger.error("SMTP failed after %d attempts: %s", self.MAX_RETRIES, last_exc)
        raise last_exc  # type: ignore[misc]

    def _send_smtp(self, msg: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        with smtplib.SMTP(self.config.server, self.config.port, timeout=30) as server:
            if self.config.use_starttls:
                server.starttls(context=context)
            server.login(self.config.username, self.config.password)
            server.sendmail(self.config.from_email, [self.config.to_email], msg.as_string())

    @staticmethod
    def _build_html(
        product_name: str,
        product_url: str,
        current_price: float,
        currency: str,
        price_in_base: Optional[float],
        base_currency: str,
        old_price: Optional[float],
        target_price: Optional[float],
        triggered_by: str,
    ) -> str:
        rows = ""
        if old_price is not None:
            delta = current_price - old_price
            sign = "-" if delta < 0 else "+"
            rows += f"<tr><td>Previous Price</td><td>${old_price:.2f}</td></tr>"
            rows += f"<tr><td>Change</td><td>{sign}${abs(delta):.2f}</td></tr>"
        if price_in_base is not None and currency != base_currency:
            rows += f"<tr><td>Approx {base_currency}</td><td>${price_in_base:.2f} {base_currency}</td></tr>"
        if target_price is not None:
            rows += f"<tr><td>Target Price</td><td>${target_price:.2f} {currency}</td></tr>"

        return f"""\
<html>
<body style="font-family:sans-serif;max-width:600px;margin:auto;">
<h2 style="color:#2d6a4f;">Sale Alert: {product_name}</h2>
<table style="border-collapse:collapse;width:100%;">
<tr style="background:#d8f3dc;">
  <td style="padding:8px;font-weight:bold;">Current Price</td>
  <td style="padding:8px;font-size:1.2em;font-weight:bold;">${current_price:.2f} {currency}</td>
</tr>
{rows}
<tr><td style="padding:8px;">Trigger</td><td style="padding:8px;">{triggered_by}</td></tr>
</table>
<p><a href="{product_url}" style="color:#2d6a4f;">View Product</a></p>
</body>
</html>"""