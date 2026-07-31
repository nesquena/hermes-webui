"""Regression tests for HTTP/1.1 write rejection framing.

A rejected POST can happen before its request body is consumed (authentication
or CSRF failure). Keeping that connection alive leaves the body bytes in
``rfile``; BaseHTTPRequestHandler then parses them as the next request method.
The connection must close after those early rejections.
"""
from types import SimpleNamespace
from typing import Any, cast


def test_auth_rejected_write_closes_connection(monkeypatch):
    import server

    handler = SimpleNamespace(
        path="/api/session/new",
        command="POST",
        close_connection=False,
    )
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(server, "check_auth", lambda _handler, _parsed: False)

    def route_must_not_run(_handler, _parsed):
        raise AssertionError("route ran after auth rejection")

    server.Handler._handle_write(cast(Any, handler), route_must_not_run)

    assert handler.close_connection is True


def test_origin_rejected_csrf_closes_connection(monkeypatch):
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler: False)

    assert routes._check_csrf(handler) is False
    assert handler.close_connection is True


def test_token_rejected_csrf_closes_connection(monkeypatch):
    import api.auth as auth
    import api.routes as routes

    handler = SimpleNamespace(headers={}, close_connection=False)
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler: True)
    monkeypatch.setattr(routes, "_is_browser_unsafe_request", lambda _handler: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "session-cookie")
    monkeypatch.setattr(auth, "verify_csrf_token", lambda _cookie, _token: False)

    assert routes._check_csrf(handler) is False
    assert handler.close_connection is True
