from typing import Dict, Optional
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

class NotificationManager:
    """Handles sending notifications via email."""
    
    def __init__(self, config: Dict):
        self.config = config
    
    def send_sale_notification(self, product_name: str, current_price: float, old_price: Optional[float] = None):
        """Send notification about a product going on sale via email."""
        self._send_email_notification(product_name, current_price, old_price)
    
    def _send_email_notification(self, product_name: str, current_price: float, old_price: Optional[float] = None):
        """Send email notification using environment variables."""
        smtp_server = os.getenv('SMTP_SERVER')
        smtp_port = int(os.getenv('SMTP_PORT', 587))
        smtp_username = os.getenv('SMTP_USERNAME')
        smtp_password = os.getenv('SMTP_PASSWORD')
        recipient_email = os.getenv('RECIPIENT_EMAIL', smtp_username)
        
        if not all([smtp_server, smtp_username, smtp_password]):
            logging.error("Email configuration missing. Please set SMTP_SERVER, SMTP_USERNAME, and SMTP_PASSWORD environment variables.")
            return
        
        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_username
            msg['To'] = recipient_email
            msg['Subject'] = f"Sale Alert: {product_name}"
            
            if old_price is not None:
                discount = ((old_price - current_price) / old_price) * 100
                body = f"""
                Great news! The price of {product_name} has dropped!
                
                Previous price: ${old_price:.2f}
                Current price: ${current_price:.2f}
                Discount: {discount:.1f}%
                
                Happy shopping!
                """
            else:
                body = f"""
                {product_name} is now available at ${current_price:.2f}
                """
            
            msg.attach(MIMEText(body, 'plain'))
            
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_username, smtp_password)
                server.send_message(msg)
            
            logging.info(f"Email notification sent for {product_name}")
        
        except Exception as e:
            logging.error(f"Failed to send email notification: {e}")