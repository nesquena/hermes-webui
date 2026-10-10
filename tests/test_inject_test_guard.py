"""Regression: inject_test endpoints fail closed unless explicitly enabled.

Behind a same-host reverse proxy (Svelte Node proxy / Tailscale Funnel) the
backend always sees peer 127.0.0.1, so a peer-IP-only guard is insufficient.
Inject routes require HERMES_WEBUI_ALLOW_INJECT_TEST plus a real loopback peer.
"""
from __future__ import annotations

from urllib.parse import urlparse

import api.routes as routes


class _FakeHandler:
    def __init__(self, peer: str = "127.0.0.1"):
        self.client_address = (peer, 12345)
        self.status = None
        self.payload = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_a, **_k):
        pass

    def end_headers(self):
        pass


def test_inject_test_denied_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_INJECT_TEST", raising=False)
    handler = _FakeHandler("127.0.0.1")
    assert routes._inject_test_allowed(handler) is False


def test_inject_test_denied_when_env_on_but_peer_not_loopback(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_INJECT_TEST", "1")
    handler = _FakeHandler("203.0.113.9")
    assert routes._inject_test_allowed(handler) is False


def test_inject_test_allowed_for_loopback_ipv4_and_ipv6(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_INJECT_TEST", "1")
    assert routes._inject_test_allowed(_FakeHandler("127.0.0.1")) is True
    assert routes._inject_test_allowed(_FakeHandler("::1")) is True


def test_approval_inject_route_returns_404_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_INJECT_TEST", raising=False)
    cap = {}

    def _j(_h, obj, *_, status=200, **__):
        cap["ok"] = obj
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    called = {"inject": False}
    monkeypatch.setattr(
        routes,
        "_handle_approval_inject",
        lambda *_a, **_k: called.__setitem__("inject", True) or True,
    )

    handler = _FakeHandler("127.0.0.1")
    routes.handle_get(handler, urlparse("/api/approval/inject_test?session_id=s1"))
    assert called["inject"] is False
    assert cap.get("status") == 404
    assert cap.get("ok", {}).get("error") == "not found"


def test_clarify_inject_route_returns_404_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_INJECT_TEST", raising=False)
    cap = {}

    def _j(_h, obj, *_, status=200, **__):
        cap["ok"] = obj
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    called = {"inject": False}
    monkeypatch.setattr(
        routes,
        "_handle_clarify_inject",
        lambda *_a, **_k: called.__setitem__("inject", True) or True,
    )

    handler = _FakeHandler("127.0.0.1")
    routes.handle_get(handler, urlparse("/api/clarify/inject_test?session_id=s1"))
    assert called["inject"] is False
    assert cap.get("status") == 404


def test_approval_inject_route_invokes_handler_when_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_INJECT_TEST", "1")
    called = {"inject": False}
    monkeypatch.setattr(
        routes,
        "_handle_approval_inject",
        lambda *_a, **_k: called.__setitem__("inject", True) or True,
    )
    handler = _FakeHandler("127.0.0.1")
    routes.handle_get(handler, urlparse("/api/approval/inject_test?session_id=s1"))
    assert called["inject"] is True


def test_updates_simulate_denied_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_UPDATE_SIMULATE", raising=False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    cap = {}

    def _j(_h, obj, *_, status=200, **__):
        cap["ok"] = obj
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    handler = _FakeHandler("127.0.0.1")
    routes.handle_get(handler, urlparse("/api/updates/check?simulate=1"))
    assert cap.get("status") == 404
    assert cap.get("ok", {}).get("error") == "not found"


def test_updates_simulate_allowed_with_env_and_loopback(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_UPDATE_SIMULATE", "1")
    monkeypatch.setattr(routes, "load_settings", lambda: {"check_for_updates": True})
    cap = {}

    def _j(_h, obj, *_, status=200, **__):
        cap["ok"] = obj
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    handler = _FakeHandler("127.0.0.1")
    routes.handle_get(handler, urlparse("/api/updates/check?simulate=1"))
    assert cap.get("status", 200) == 200
    assert cap["ok"]["webui"]["behind"] == 3
