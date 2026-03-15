"""Tests for Phase 1 security features: auth, CSRF, rate limiting, input validation."""
import os
from unittest.mock import patch

HEADER = "name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours\n"


def write_products_csv(path, rows):
    lines = [
        ",".join(map(lambda v: "" if v is None else str(v), r)) for r in rows
    ]
    path.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def make_client(tmp_path, api_key=None, api_key_read_required=False):
    data_dir = tmp_path
    products_csv = data_dir / "products.csv"
    state_file = data_dir / "state.json"
    history_db = data_dir / "history.db"

    write_products_csv(
        products_csv,
        [["Widget", "https://example.com/w", "", "", "#price", "true", 24]],
    )
    state_file.write_text("{}", encoding="utf-8")

    os.environ["PRODUCTS_CSV"] = str(products_csv)
    os.environ["STATE_FILE"] = str(state_file)
    os.environ["HISTORY_DB"] = str(history_db)

    if api_key:
        os.environ["API_KEY"] = api_key
    elif "API_KEY" in os.environ:
        del os.environ["API_KEY"]

    if api_key_read_required:
        os.environ["API_KEY_READ_REQUIRED"] = "1"
    elif "API_KEY_READ_REQUIRED" in os.environ:
        del os.environ["API_KEY_READ_REQUIRED"]

    from sale_monitor.web.app import create_app
    app = create_app()
    return app.test_client()


# ── API Key Auth ──────────────────────────────────────────────────────────────


def test_post_without_api_key_returns_401(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    resp = client.post(
        "/api/product/delete",
        json={"url": "https://example.com/w"},
    )
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Unauthorized"


def test_post_with_valid_api_key_header(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    resp = client.post(
        "/api/product/delete",
        json={"url": "https://example.com/w"},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 200


def test_post_with_valid_api_key_query_param(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    resp = client.post(
        "/api/product/delete?api_key=test-secret-key",
        json={"url": "https://example.com/w"},
    )
    assert resp.status_code == 200


def test_post_with_wrong_api_key_returns_401(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    resp = client.post(
        "/api/product/delete",
        json={"url": "https://example.com/w"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_get_without_api_key_allowed_by_default(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    resp = client.get("/api/products")
    assert resp.status_code == 200


def test_get_requires_key_when_read_required(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key", api_key_read_required=True)
    resp = client.get("/api/products")
    assert resp.status_code == 401


def test_get_with_key_when_read_required(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key", api_key_read_required=True)
    resp = client.get("/api/products", headers={"X-API-Key": "test-secret-key"})
    assert resp.status_code == 200


def test_no_api_key_env_means_auth_disabled(tmp_path):
    client = make_client(tmp_path, api_key=None)
    resp = client.post(
        "/api/product/delete",
        json={"url": "https://example.com/w"},
    )
    assert resp.status_code == 200


def test_template_pages_not_gated_by_auth(tmp_path):
    client = make_client(tmp_path, api_key="test-secret-key")
    for path in ("/", "/manage", "/alerts", "/compare", "/failures"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


# ── CSRF Protection ──────────────────────────────────────────────────────────


def test_csrf_rejects_form_encoded_post(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/delete",
        data="url=https://example.com/w",
        content_type="application/x-www-form-urlencoded",
    )
    assert resp.status_code == 400
    assert "Content-Type" in resp.get_json()["error"]


def test_csrf_allows_json_post(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/delete",
        json={"url": "https://example.com/w"},
    )
    assert resp.status_code == 200


def test_csrf_allows_empty_body_post(tmp_path):
    """POST with no body (e.g. check-all) should be allowed."""
    client = make_client(tmp_path)
    resp = client.post("/api/products/check-all")
    # Should not be 400 (CSRF), may fail for other reasons
    assert resp.status_code != 400


# ── Input Validation ─────────────────────────────────────────────────────────


def test_add_product_rejects_ftp_url(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/add",
        json={"name": "Bad", "url": "ftp://evil.com/file"},
    )
    assert resp.status_code == 400
    assert "http" in resp.get_json()["error"].lower()


def test_add_product_rejects_long_name(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/add",
        json={"name": "A" * 501, "url": "https://example.com/long"},
    )
    assert resp.status_code == 400
    assert "500" in resp.get_json()["error"]


def test_add_product_rejects_negative_price(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/add",
        json={
            "name": "Neg",
            "url": "https://example.com/neg",
            "target_price": "-5.0",
        },
    )
    assert resp.status_code == 400
    assert "negative" in resp.get_json()["error"].lower()


def test_add_product_rejects_huge_cooldown(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/add",
        json={
            "name": "Huge",
            "url": "https://example.com/huge",
            "notification_cooldown_hours": "9999",
        },
    )
    assert resp.status_code == 400
    assert "8760" in resp.get_json()["error"]


def test_add_product_accepts_valid_input(tmp_path):
    client = make_client(tmp_path)
    resp = client.post(
        "/api/product/add",
        json={
            "name": "Good Product",
            "url": "https://example.com/good",
            "target_price": "19.99",
            "notification_cooldown_hours": "24",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


# ── Error Sanitization ───────────────────────────────────────────────────────


def test_error_responses_dont_leak_internals(tmp_path):
    """Trigger a server error and verify the response doesn't contain file paths or stack traces."""
    client = make_client(tmp_path)
    # Break the state file to cause an error
    state_path = os.environ["STATE_FILE"]
    os.remove(state_path)
    os.mkdir(state_path)  # Replace file with directory -> causes OSError

    resp = client.get("/api/products")
    assert resp.status_code == 500
    body = resp.get_json()
    # Should NOT contain the actual file path
    assert str(tmp_path) not in body.get("error", "")
    # Should be a generic message
    assert "internal" in body.get("error", "").lower() or "error" in body.get("error", "").lower()


# ── File Locking ─────────────────────────────────────────────────────────────


def test_file_lock_context_manager(tmp_path):
    from sale_monitor.storage.file_lock import FileLock

    lock_target = str(tmp_path / "test_file")
    lock = FileLock(lock_target)

    with lock:
        # Lock is held; second acquire from same thread should block
        # but we just test context manager works without error
        assert lock.lock_fd is not None

    # After exit, fd should be released
    assert lock.lock_fd is None


def test_file_lock_acquire_release(tmp_path):
    from sale_monitor.storage.file_lock import FileLock

    lock_target = str(tmp_path / "test_file")
    lock = FileLock(lock_target)

    lock.acquire()
    assert lock.lock_fd is not None
    lock.release()
    assert lock.lock_fd is None
