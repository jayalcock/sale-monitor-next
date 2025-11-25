from sale_monitor.services.notifications import NotificationManager, SmtpConfig


def make_cfg(enable=True, starttls=True):
    return SmtpConfig(
        server="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        from_email="from@example.com",
        to_email="to@example.com",
        enable=enable,
        use_starttls=starttls,
    )


def test_send_email_uses_starttls_and_login(mocker):
    # Arrange
    cfg = make_cfg(enable=True, starttls=True)
    notifier = NotificationManager(cfg)

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    smtp_cm = mock_smtp_cls.return_value
    server = mocker.MagicMock()
    smtp_cm.__enter__.return_value = server

    # Act
    notifier.send_sale_notification(
        product_name="Test Product",
        product_url="https://example.com/p/1",
        current_price=123.45,
        old_price=150.00,
        target_price=120.00,
        triggered_by="target_price",
    )

    # Assert: context manager called
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("user@example.com", "secret")
    assert server.sendmail.call_count == 1

    # Assert: email contents include subject, product, URL, price lines
    args, kwargs = server.sendmail.call_args
    assert args[0] == "from@example.com"
    assert args[1] == ["to@example.com"]
    raw_msg = args[2]

    assert "Subject: Sale Monitor: Test Product at $123.45" in raw_msg
    assert "Product: Test Product" in raw_msg
    assert "URL: https://example.com/p/1" in raw_msg


def test_send_email_without_starttls(mocker):
    # Arrange
    cfg = make_cfg(enable=True, starttls=False)
    notifier = NotificationManager(cfg)

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)
    smtp_cm = mock_smtp_cls.return_value
    server = mocker.MagicMock()
    smtp_cm.__enter__.return_value = server

    # Act
    notifier.send_sale_notification(
        product_name="NoTLS Product",
        product_url="https://example.com/p/2",
        current_price=50.00,
        old_price=None,
        target_price=None,
        triggered_by="rule",
    )

    # Assert
    server.starttls.assert_not_called()
    server.login.assert_called_once_with("user@example.com", "secret")
    server.sendmail.assert_called_once()


def test_send_email_disabled_does_nothing(mocker):
    # Arrange
    cfg = make_cfg(enable=False, starttls=True)
    notifier = NotificationManager(cfg)

    mock_smtp_cls = mocker.patch("smtplib.SMTP", autospec=True)

    # Act
    notifier.send_sale_notification(
        product_name="Disabled Product",
        product_url="https://example.com/p/3",
        current_price=10.00,
    )

    # Assert: SMTP never instantiated
    mock_smtp_cls.assert_not_called()