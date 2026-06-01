"""Route tests for the Weixin (微信 / 个人微信) platform config + QR login endpoints.

Mirrors tests/test_wecom_routes.py: calls ``routes.handle_get`` /
``routes.handle_post`` directly with a stub handler, monkeypatching the in-scope
response helpers (``j`` / ``bad``), ``read_body`` to inject a JSON body, and
``_check_csrf``. The weixin logic in ``api.platforms.weixin`` is monkeypatched —
these tests assert routing, status codes, secret hygiene, and restart honoring.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import routes
from api.platforms import weixin


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


# ── GET /api/platforms/weixin ────────────────────────────────────────────────


def test_get_returns_get_config_payload(monkeypatch, captured):
    sentinel = {"configured": True, "account_id": "bot_1", "token_set": True}
    monkeypatch.setattr(weixin, "get_config", lambda: sentinel)

    handled = routes.handle_get(object(), SimpleNamespace(path="/api/platforms/weixin"))

    assert handled is not False
    assert captured["payload"] == sentinel
    assert captured["status"] == 200


# ── GET /api/platforms/weixin/login/status ───────────────────────────────────


def test_status_dispatches_login_id_from_query(monkeypatch, captured):
    calls: dict = {}

    def fake_poll(login_id):
        calls["login_id"] = login_id
        return {"state": "waiting"}

    monkeypatch.setattr(weixin, "poll_status", fake_poll)

    routes.handle_get(
        object(),
        SimpleNamespace(path="/api/platforms/weixin/login/status", query="login_id=lid42"),
    )

    assert calls["login_id"] == "lid42"
    assert captured["payload"]["state"] == "waiting"
    assert captured["status"] == 200


def test_status_confirmed_never_leaks_token(monkeypatch, captured):
    # poll_status returns only account_id, never a token — assert the route
    # serialization carries no token field even if one slipped through.
    monkeypatch.setattr(
        weixin, "poll_status", lambda login_id: {"state": "confirmed", "account_id": "bot_9"}
    )
    routes.handle_get(
        object(),
        SimpleNamespace(path="/api/platforms/weixin/login/status", query="login_id=lid1"),
    )
    assert "token" not in captured["serialized"]
    assert captured["payload"]["state"] == "confirmed"


# ── POST /api/platforms/weixin/login/start ───────────────────────────────────


def test_login_start_dispatches(monkeypatch, captured):
    called: dict = {"n": 0}

    def fake_start():
        called["n"] += 1
        return {"login_id": "abc", "state": "waiting", "qr_url": "https://scan"}

    monkeypatch.setattr(weixin, "start_login", fake_start)
    _set_body(monkeypatch, {})

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/weixin/login/start")
    )

    assert called["n"] == 1
    assert captured["payload"]["login_id"] == "abc"
    assert captured["status"] == 200


def test_login_start_error_passthrough(monkeypatch, captured):
    monkeypatch.setattr(weixin, "start_login", lambda: {"error": "unavailable"})
    _set_body(monkeypatch, {})

    routes.handle_post(
        object(), SimpleNamespace(path="/api/platforms/weixin/login/start")
    )

    assert captured["payload"]["error"] == "unavailable"
    assert captured["status"] == 200


# ── POST /api/platforms/weixin (save) ────────────────────────────────────────


def test_save_dispatches_and_returns_result(monkeypatch, captured):
    calls: dict = {}

    def fake_save(payload):
        calls["payload"] = payload
        return {"saved": True, "fields": ["WEIXIN_DM_POLICY"]}

    monkeypatch.setattr(weixin, "save", fake_save)
    monkeypatch.setattr(
        weixin, "restart_gateway", lambda: pytest.fail("restart must not be called")
    )
    body = {"dm_policy": "open", "group_policy": "disabled"}
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/weixin"))

    assert calls["payload"] is body
    assert captured["status"] == 200
    assert captured["payload"]["saved"] is True


def test_save_config_error_returns_400(monkeypatch, captured):
    def raise_config_error(_payload):
        raise weixin.WeixinConfigError("dm_policy must be ...")

    monkeypatch.setattr(weixin, "save", raise_config_error)
    _set_body(monkeypatch, {"dm_policy": "bad"})

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/weixin"))

    assert captured["status"] == 400
    assert "dm_policy" in captured["payload"]["error"]


def test_restart_true_calls_restart_gateway(monkeypatch, captured):
    called: dict = {"n": 0}

    def fake_restart():
        called["n"] += 1
        return {"ok": True, "detail": "gateway restarted"}

    monkeypatch.setattr(
        weixin, "save", lambda payload: {"saved": True, "fields": ["WEIXIN_DM_POLICY"]}
    )
    monkeypatch.setattr(weixin, "restart_gateway", fake_restart)
    _set_body(monkeypatch, {"dm_policy": "open", "restart": True})

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/weixin"))

    assert called["n"] == 1
    assert captured["payload"]["restart"] == {"ok": True, "detail": "gateway restarted"}


@pytest.mark.parametrize("body_extra", [{}, {"restart": False}, {"restart": "true"}])
def test_restart_not_called_unless_strictly_true(monkeypatch, captured, body_extra):
    monkeypatch.setattr(
        weixin, "save", lambda payload: {"saved": True, "fields": ["WEIXIN_DM_POLICY"]}
    )
    monkeypatch.setattr(
        weixin, "restart_gateway", lambda: pytest.fail("restart must not be called")
    )
    body = {"dm_policy": "open"}
    body.update(body_extra)
    _set_body(monkeypatch, body)

    routes.handle_post(object(), SimpleNamespace(path="/api/platforms/weixin"))

    assert captured["payload"]["saved"] is True
    assert "restart" not in captured["payload"]
