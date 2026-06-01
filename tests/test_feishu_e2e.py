"""End-to-end tests for the Feishu (飞书 / Lark) WebUI config integration.

These exercise the full live HTTP stack against the isolated session test
server (see tests/conftest.py — TEST_STATE_DIR, never the real ~/.hermes).
They cover Tasks 1–2 endpoints:

  GET  /api/platforms/feishu            → masked read-back
  POST /api/platforms/feishu            → save (secret-only-if-provided)
  POST /api/platforms/feishu/validate   → structured probe result

By design this never posts ``restart: true`` — the restart gating was
unit-tested in Task 2 and a real ``hermes gateway restart`` must never run
against the user's machine. The real-credentials manual smoke test is left to
the user (documented in the README).
"""
import json
import pathlib
import urllib.error
import urllib.request

import pytest

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _server_hermes_home() -> pathlib.Path:
    """Derive the hermes home the test server is actually using from its own
    /api/onboarding/status response (env_path's parent)."""
    data, _ = get("/api/onboarding/status")
    env_path = data.get("system", {}).get("env_path", "")
    if env_path:
        return pathlib.Path(env_path).parent
    # Fallback (mirrors test_onboarding_mvp)
    return pathlib.Path.home() / ".hermes" / "webui-mvp-test"


def _env_text() -> str:
    return (_server_hermes_home() / ".env").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clean_feishu_env():
    """Isolate each test by unlinking .env before and after, so FEISHU_* keys
    never leak between tests."""
    env_file = _server_hermes_home() / ".env"
    env_file.unlink(missing_ok=True)
    yield
    env_file.unlink(missing_ok=True)


# ── Defaults on a clean .env ────────────────────────────────────────────────


def test_feishu_defaults_on_clean_env():
    data, status = get("/api/platforms/feishu")
    assert status == 200
    assert data["configured"] is False
    assert data["app_secret_set"] is False
    assert data["app_id"] == ""
    assert data["domain"] == "feishu"
    assert data["connection_mode"] == "websocket"


# ── Save writes keys; raw secret never echoed back ──────────────────────────


def test_feishu_save_writes_keys_and_hides_secret():
    body = {
        "app_id": "cli_test123",
        "app_secret": "secret_value_xyz",
        "domain": "feishu",
        "connection_mode": "websocket",
        "group_policy": "open",
        "require_mention": True,
        "allow_all_users": False,
    }
    data, status = post("/api/platforms/feishu", body)
    assert status == 200
    assert data["saved"] is True
    # The raw secret must NOT appear anywhere in the response JSON.
    assert "secret_value_xyz" not in json.dumps(data)

    env_text = _env_text()
    assert "FEISHU_APP_ID=cli_test123" in env_text
    assert "FEISHU_APP_SECRET=secret_value_xyz" in env_text
    assert "FEISHU_DOMAIN=feishu" in env_text


# ── Masking on read-back ────────────────────────────────────────────────────


def test_feishu_masking_on_read_back():
    post(
        "/api/platforms/feishu",
        {
            "app_id": "cli_test123",
            "app_secret": "secret_value_xyz",
            "domain": "feishu",
            "connection_mode": "websocket",
        },
    )
    data, status = get("/api/platforms/feishu")
    assert status == 200
    assert data["configured"] is True
    assert data["app_secret_set"] is True
    assert data["app_id"] == "cli_test123"
    assert "secret_value_xyz" not in json.dumps(data)


# ── Secret preserved when client echoes the masked sentinel ─────────────────


def test_feishu_secret_preserved_when_masked():
    post(
        "/api/platforms/feishu",
        {
            "app_id": "cli_test123",
            "app_secret": "secret_value_xyz",
            "domain": "feishu",
            "connection_mode": "websocket",
        },
    )
    # Re-save with the masked sentinel for the secret and a changed domain.
    data, status = post(
        "/api/platforms/feishu",
        {
            "app_id": "cli_test123",
            "app_secret": "__FEISHU_SECRET_SET__",
            "domain": "lark",
            "connection_mode": "websocket",
        },
    )
    assert status == 200
    assert data["saved"] is True

    env_text = _env_text()
    # Secret untouched, domain updated.
    assert "FEISHU_APP_SECRET=secret_value_xyz" in env_text
    assert "FEISHU_DOMAIN=lark" in env_text


# ── Webhook fields are written only in webhook mode ─────────────────────────


def test_feishu_webhook_fields_gated():
    data, status = post(
        "/api/platforms/feishu",
        {
            "app_id": "cli_test123",
            "app_secret": "secret_value_xyz",
            "domain": "feishu",
            "connection_mode": "webhook",
            "webhook_port": "9999",
        },
    )
    assert status == 200
    assert data["saved"] is True

    env_text = _env_text()
    assert "FEISHU_WEBHOOK_PORT=9999" in env_text


def test_feishu_websocket_save_omits_webhook_keys():
    post(
        "/api/platforms/feishu",
        {
            "app_id": "cli_test123",
            "app_secret": "secret_value_xyz",
            "domain": "feishu",
            "connection_mode": "websocket",
        },
    )
    env_text = _env_text()
    assert "FEISHU_CONNECTION_MODE=websocket" in env_text
    assert "FEISHU_WEBHOOK_PORT" not in env_text


# ── Validate path returns structured JSON ───────────────────────────────────


def test_feishu_validate_returns_structured_result():
    data, status = post(
        "/api/platforms/feishu/validate",
        {"app_id": "x", "app_secret": "y", "domain": "feishu"},
    )
    assert status == 200
    # Shape is reliable across the sandbox; ok is False whether the agent is
    # unavailable or the probe simply fails for the bogus creds. Don't assert a
    # specific error string (depends on whether the lark SDK is importable).
    assert "ok" in data
    assert data["ok"] is False
