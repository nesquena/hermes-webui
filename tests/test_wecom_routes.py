"""Route tests for the WeCom (企业微信) platform config endpoints.

Mirrors tests/test_feishu_routes.py: calls ``routes.handle_get`` /
``routes.handle_post`` directly with a stub handler, monkeypatching the in-scope
response helpers (``j`` / ``bad``) to capture what the route would have sent,
``read_body`` to inject a JSON body, and ``_check_csrf`` to bypass central CSRF
gating (verified elsewhere). The WeCom logic in ``api.platforms.wecom`` is
monkeypatched here so these tests assert *routing*, *status codes*, *secret
hygiene*, and *restart honoring* — not the module internals.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import routes
from api.platforms import wecom


@pytest.fixture
def captured(monkeypatch):
    box: dict = {}

    def fake_j(_handler, payload, status: int = 200, extra_headers=None):
        box["payload"] = payload
        box["status"] = status
        box["serialized"] = json.dumps(payload, ensure_ascii=False)
        return None

    def fake_bad(_handler, msg, status: int = 400):
        return fake_j(_handler, {"error": msg}, status=status)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    return box


def _set_body(monkeypatch, body: dict) -> None:
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)


# ── GET /api/platforms/wecom ─────────────────────────────────────────────────


def test_get_returns_get_config_payload(monkeypatch, captured):
    sentinel = {
        "configured": True,
        "mode": "wecom",
        "bot_id": "bot_abc",
        "secret_set": True,
    }
    monkeypatch.setattr(wecom, "get_config", lambda: sentinel)

    handled = routes.handle_get(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert handled is not False  # route was matched
    assert captured["payload"] == sentinel
    assert captured["status"] == 200


# ── POST /api/platforms/wecom/validate ───────────────────────────────────────


def test_validate_dispatches_to_module(monkeypatch, captured):
    calls: dict = {}

    def fake_validate(payload):
        calls["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(wecom, "validate", fake_validate)
    body = {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret"}
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom/validate"))

    assert calls["payload"] is body  # body passed straight through
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True


def test_validate_never_echoes_the_secret_sent(monkeypatch, captured):
    monkeypatch.setattr(
        wecom, "validate", lambda payload: {"ok": False, "error": "invalid credentials"}
    )
    _set_body(
        monkeypatch, {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret"}
    )

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom/validate"))

    assert "supersecret" not in captured["serialized"]


# ── POST /api/platforms/wecom (save) ─────────────────────────────────────────


def test_save_dispatches_and_returns_result(monkeypatch, captured):
    calls: dict = {}

    def fake_save(payload):
        calls["payload"] = payload
        return {"saved": True, "fields": ["WECOM_BOT_ID", "WECOM_SECRET"]}

    monkeypatch.setattr(wecom, "save", fake_save)
    monkeypatch.setattr(
        wecom, "restart_gateway", lambda: pytest.fail("restart must not be called")
    )
    body = {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret"}
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert calls["payload"] is body
    assert captured["status"] == 200
    assert captured["payload"]["saved"] is True


def test_save_never_echoes_the_secret_sent(monkeypatch, captured):
    monkeypatch.setattr(
        wecom, "save", lambda payload: {"saved": True, "fields": ["WECOM_SECRET"]}
    )
    _set_body(
        monkeypatch, {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret"}
    )

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert "supersecret" not in captured["serialized"]


def test_save_config_error_returns_400(monkeypatch, captured):
    def raise_config_error(_payload):
        raise wecom.WecomConfigError("WECOM_BOT_ID is required")

    monkeypatch.setattr(wecom, "save", raise_config_error)
    _set_body(monkeypatch, {"mode": "wecom", "secret": "supersecret"})

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert captured["status"] == 400
    assert "WECOM_BOT_ID is required" in captured["payload"]["error"]
    assert "supersecret" not in captured["serialized"]


# ── restart honoring ─────────────────────────────────────────────────────────


def test_restart_true_calls_restart_gateway_and_includes_result(monkeypatch, captured):
    called: dict = {"n": 0}

    def fake_restart():
        called["n"] += 1
        return {"ok": True, "detail": "gateway restarted"}

    monkeypatch.setattr(
        wecom, "save", lambda payload: {"saved": True, "fields": ["WECOM_BOT_ID"]}
    )
    monkeypatch.setattr(wecom, "restart_gateway", fake_restart)
    _set_body(
        monkeypatch,
        {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret", "restart": True},
    )

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert called["n"] == 1
    assert captured["payload"]["restart"] == {"ok": True, "detail": "gateway restarted"}
    assert captured["status"] == 200


@pytest.mark.parametrize("body_extra", [{}, {"restart": False}, {"restart": "true"}])
def test_restart_not_called_unless_strictly_true(monkeypatch, captured, body_extra):
    monkeypatch.setattr(
        wecom, "save", lambda payload: {"saved": True, "fields": ["WECOM_BOT_ID"]}
    )
    monkeypatch.setattr(
        wecom,
        "restart_gateway",
        lambda: pytest.fail("restart_gateway must not be called"),
    )
    body = {"mode": "wecom", "bot_id": "bot_abc", "secret": "supersecret"}
    body.update(body_extra)
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/wecom"))

    assert captured["payload"]["saved"] is True
    assert "restart" not in captured["payload"]
