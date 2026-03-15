"""Tests for SMTP retry logic and HTML email templates (Phase 2)."""
import smtplib
from unittest.mock import MagicMock

from sale_monitor.services.notifications import NotificationManager, SmtpConfig


def make_cfg():
    return SmtpConfig(
        server="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        from_email="from@example.com",
        to_email="to@example.com",
        enable=True,
        use_starttls=True,
    )


SEND_KWARGS = dict(
    product_name="Test Widget",
    product_url="https://example.com/widget",
    current_price=49.99,
    old_price=79.99,
    target_price=50.00,
    triggered_by="target_price",
)


# ── Retry Logic ──────────────────────────────────────────────────────────────


def test_retry_succeeds_on_third_attempt(mocker):
    """SMTP fails twice then succeeds — should not raise."""
    mocker.patch("time.sleep")  # don't actually sleep
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise smtplib.SMTPServerDisconnected("Connection lost")

    server.sendmail.side_effect = side_effect

    notifier.send_sale_notification(**SEND_KWARGS)
    assert call_count == 3


def test_retry_all_fail_raises(mocker):
    """SMTP fails 3 times — should raise after exhausting retries."""
    mocker.patch("time.sleep")
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server
    server.sendmail.side_effect = smtplib.SMTPServerDisconnected("Connection lost")

    try:
        notifier.send_sale_notification(**SEND_KWARGS)
        assert False, "Expected exception"
    except smtplib.SMTPServerDisconnected:
        pass

    assert server.sendmail.call_count == 3


def test_retry_logs_each_attempt(mocker):
    """Each retry attempt should be logged as a warning."""
    mocker.patch("time.sleep")
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    calls = []

    def side_effect(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("timed out")

    server.sendmail.side_effect = side_effect

    mock_warn = mocker.patch("sale_monitor.services.notifications.logger.warning")
    notifier.send_sale_notification(**SEND_KWARGS)

    # Two retries → two warning logs
    assert mock_warn.call_count == 2


def test_no_retry_on_first_success(mocker):
    """When SMTP succeeds on first try, no retry happens."""
    mocker.patch("time.sleep")
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    notifier.send_sale_notification(**SEND_KWARGS)
    assert server.sendmail.call_count == 1


# ── HTML Email Template ──────────────────────────────────────────────────────


def test_email_is_multipart_alternative(mocker):
    """Email should be multipart/alternative with plain text + HTML parts."""
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    notifier.send_sale_notification(**SEND_KWARGS)

    raw_msg = server.sendmail.call_args[0][2]
    assert "multipart/alternative" in raw_msg
    assert "text/plain" in raw_msg
    assert "text/html" in raw_msg


def test_html_body_contains_key_elements(mocker):
    """HTML body should contain product name, price, and link."""
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    notifier.send_sale_notification(**SEND_KWARGS)

    raw_msg = server.sendmail.call_args[0][2]
    assert "Test Widget" in raw_msg
    assert "$49.99" in raw_msg
    assert "https://example.com/widget" in raw_msg
    assert "View Product" in raw_msg
    assert "$79.99" in raw_msg  # old price


def test_html_body_currency_conversion(mocker):
    """HTML body should show base-currency conversion when currencies differ."""
    notifier = NotificationManager(make_cfg())

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = server

    notifier.send_sale_notification(
        product_name="USD Item",
        product_url="https://example.com/usd",
        current_price=39.99,
        currency="USD",
        price_in_base=54.50,
        base_currency="CAD",
    )

    raw_msg = server.sendmail.call_args[0][2]
    assert "$54.50 CAD" in raw_msg
