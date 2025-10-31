import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional


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
    def __init__(self, config: SmtpConfig):
        self.config = config

    def send_sale_notification(
        self,
        product_name: str,
        product_url: str,
        current_price: float,
        old_price: Optional[float] = None,
        target_price: Optional[float] = None,
        triggered_by: str = "target",
    ) -> None:
        if not self.config.enable:
            return

        subject = f"Sale Monitor: {product_name} at ${current_price:.2f}"
        lines = [
            f"Product: {product_name}",
            f"URL: {product_url}",
            f"Current Price: ${current_price:.2f}",
        ]
        if old_price is not None:
            lines.append(f"Previous Price: ${old_price:.2f}")
            delta = current_price - old_price
            lines.append(f"Change: {'-' if delta < 0 else '+'}${abs(delta):.2f}")
        if target_price is not None:
            lines.append(f"Target Price: ${target_price:.2f}")
        lines.append(f"Trigger: {triggered_by}")
        body = "\n".join(lines)

        msg = MIMEMultipart()
        msg["From"] = self.config.from_email
        msg["To"] = self.config.to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        context = ssl.create_default_context()

        with smtplib.SMTP(self.config.server, self.config.port, timeout=30) as server:
            if self.config.use_starttls:
                server.starttls(context=context)
            server.login(self.config.username, self.config.password)
            server.sendmail(self.config.from_email, [self.config.to_email], msg.as_string())