"""Route tests for the Feishu (飞书 / Lark) platform config endpoints (Task 2).

Approach: call ``routes.handle_get`` / ``routes.handle_post`` directly with a
stub handler, monkeypatching the in-scope response helpers (``j`` / ``bad``) to
capture what the route would have sent, ``read_body`` to inject a JSON body, and
``_check_csrf`` to bypass central CSRF gating (which is verified elsewhere).

The actual Feishu logic lives in ``api.platforms.feishu`` (Task 1) and is
monkeypatched here so these tests assert *routing*, *status codes*, *secret
hygiene*, and *restart honoring* — not the module internals.

This direct-dispatch style mirrors ``tests/test_auxiliary_models_settings.py``
and is the only reliable way to mock the module, because the live test server
runs in a separate process where in-process monkeypatching has no effect.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import routes
from api.platforms import feishu


@pytest.fixture
def captured(monkeypatch):
    """Patch routes.j / routes.bad to capture the response, and bypass CSRF.

    Returns a dict that, after a route call, holds the captured ``payload``,
    ``status`` and the ``serialized`` JSON text (so tests can scan for leaked
    secrets the way a real client would see them over the wire).
    """
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


# ── GET /api/platforms/feishu ────────────────────────────────────────────────


def test_get_returns_get_config_payload(monkeypatch, captured):
    sentinel = {
        "configured": True,
        "app_id": "cli_abc",
        "app_secret_set": True,
        "domain": "feishu",
    }
    monkeypatch.setattr(feishu, "get_config", lambda: sentinel)

    handled = routes.handle_get(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert handled is not False  # route was matched (None == handled)
    assert captured["payload"] == sentinel
    assert captured["status"] == 200


# ── POST /api/platforms/feishu/validate ──────────────────────────────────────


def test_validate_dispatches_to_module(monkeypatch, captured):
    calls: dict = {}

    def fake_validate(app_id, app_secret, domain):
        calls["args"] = (app_id, app_secret, domain)
        return {"ok": True, "bot_name": "MyBot", "bot_open_id": "ou_x"}

    monkeypatch.setattr(feishu, "validate", fake_validate)
    _set_body(
        monkeypatch,
        {"app_id": "  cli_abc  ", "app_secret": "supersecret", "domain": "lark"},
    )

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/feishu/validate")
    )

    assert calls["args"] == ("cli_abc", "supersecret", "lark")
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True


def test_validate_defaults_domain_to_feishu(monkeypatch, captured):
    calls: dict = {}
    monkeypatch.setattr(
        feishu,
        "validate",
        lambda app_id, app_secret, domain: calls.setdefault(
            "args", (app_id, app_secret, domain)
        )
        or {"ok": False, "error": "x"},
    )
    _set_body(monkeypatch, {"app_id": "cli_abc", "app_secret": "supersecret"})

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/feishu/validate")
    )

    assert calls["args"][2] == "feishu"


def test_validate_never_echoes_the_secret_sent(monkeypatch, captured):
    # Even if a buggy module returned the secret, the route must be the place we
    # assert hygiene; here the module returns only ok/error, mirroring Task 1.
    monkeypatch.setattr(
        feishu, "validate", lambda *a: {"ok": False, "error": "invalid credentials"}
    )
    _set_body(
        monkeypatch, {"app_id": "cli_abc", "app_secret": "supersecret", "domain": "feishu"}
    )

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/feishu/validate")
    )

    assert "supersecret" not in captured["serialized"]


# ── POST /api/platforms/feishu (save) ────────────────────────────────────────


def test_save_dispatches_and_returns_result(monkeypatch, captured):
    calls: dict = {}

    def fake_save(payload):
        calls["payload"] = payload
        return {"saved": True, "fields": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]}

    monkeypatch.setattr(feishu, "save", fake_save)
    monkeypatch.setattr(
        feishu, "restart_gateway", lambda: pytest.fail("restart must not be called")
    )
    body = {"app_id": "cli_abc", "app_secret": "supersecret", "domain": "feishu"}
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert calls["payload"] is body  # body passed straight through to save()
    assert captured["status"] == 200
    assert captured["payload"]["saved"] is True


def test_save_never_echoes_the_secret_sent(monkeypatch, captured):
    monkeypatch.setattr(
        feishu, "save", lambda payload: {"saved": True, "fields": ["FEISHU_APP_SECRET"]}
    )
    _set_body(
        monkeypatch,
        {"app_id": "cli_abc", "app_secret": "supersecret", "domain": "feishu"},
    )

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert "supersecret" not in captured["serialized"]


def test_save_config_error_returns_400(monkeypatch, captured):
    def raise_config_error(_payload):
        raise feishu.FeishuConfigError("FEISHU_APP_ID is required")

    monkeypatch.setattr(feishu, "save", raise_config_error)
    _set_body(monkeypatch, {"domain": "feishu"})

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert captured["status"] == 400
    assert "FEISHU_APP_ID is required" in captured["payload"]["error"]
    # A 400 path must not leak any secret the client may have sent either.
    assert "supersecret" not in captured["serialized"]


# ── restart honoring ─────────────────────────────────────────────────────────


def test_restart_true_calls_restart_gateway_and_includes_result(monkeypatch, captured):
    called: dict = {"n": 0}

    def fake_restart():
        called["n"] += 1
        return {"ok": True, "detail": "gateway restarted"}

    monkeypatch.setattr(
        feishu, "save", lambda payload: {"saved": True, "fields": ["FEISHU_APP_ID"]}
    )
    monkeypatch.setattr(feishu, "restart_gateway", fake_restart)
    _set_body(
        monkeypatch,
        {
            "app_id": "cli_abc",
            "app_secret": "supersecret",
            "domain": "feishu",
            "restart": True,
        },
    )

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert called["n"] == 1
    assert captured["payload"]["saved"] is True
    assert captured["payload"]["restart"] == {"ok": True, "detail": "gateway restarted"}
    assert captured["status"] == 200


@pytest.mark.parametrize("body_extra", [{}, {"restart": False}, {"restart": "true"}])
def test_restart_not_called_unless_strictly_true(monkeypatch, captured, body_extra):
    """Only ``restart is True`` triggers a restart (not falsy / not truthy-strings)."""
    monkeypatch.setattr(
        feishu, "save", lambda payload: {"saved": True, "fields": ["FEISHU_APP_ID"]}
    )
    monkeypatch.setattr(
        feishu,
        "restart_gateway",
        lambda: pytest.fail("restart_gateway must not be called"),
    )
    body = {"app_id": "cli_abc", "app_secret": "supersecret", "domain": "feishu"}
    body.update(body_extra)
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/feishu"))

    assert captured["payload"]["saved"] is True
    assert "restart" not in captured["payload"]
