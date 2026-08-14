"""Regression tests for HTTP/1.1 write rejection framing.

A rejected POST can happen before its request body is consumed (authentication
or CSRF failure). Keeping that connection alive leaves the body bytes in
``rfile``; BaseHTTPRequestHandler then parses them as the next request method.
The connection must close after those early rejections.
"""
from types import SimpleNamespace
from typing import Any, cast


def test_auth_rejected_write_closes_connection(monkeypatch):
    import api.auth as auth
    import server

    handler = SimpleNamespace(
        path="/api/session/new",
        command="POST",
        close_connection=False,
    )
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: False)

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


def test_sidecar_provenance_rejected_closes_connection(monkeypatch):
    """Sidecar proxy provenance rejection also runs before read_body()."""
    import api.routes as routes

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="POST",
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(
        handler, SimpleNamespace(path=handler.path, query=""), "POST", read_request_body=True
    )

    assert handler.close_connection is True


def test_sidecar_get_provenance_rejected_keeps_connection(monkeypatch):
    """A body-less GET rejection has nothing unread — keep-alive must survive.

    Closing here would kill a pooled client's connection for no framing
    reason (the deep-review gate's MUST-FIX 2).
    """
    import api.routes as routes

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="GET",
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(handler, SimpleNamespace(path=handler.path, query=""), "GET")

    assert handler.close_connection is False


def test_csp_report_rate_limited_closes_connection(monkeypatch):
    """A rate-limited CSP report is dropped before its body is read."""
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "_csp_report_rate_limited", lambda _handler: True)
    monkeypatch.setattr(routes, "_send_no_content", lambda _handler: True)

    assert routes._handle_csp_report(handler) is True
    assert handler.close_connection is True


def test_health_restart_closes_connection(monkeypatch):
    """health/restart never consumes its body on any outcome."""
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "restart_active_profile_gateway", lambda: {"status": "completed"})
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: True)

    routes._handle_health_restart(handler)
    assert handler.close_connection is True


def test_upload_oversize_rejects_before_read(monkeypatch):
    """An oversized upload's 413 fires before rfile.read() — connection closes."""
    import api.upload as upload

    handler = SimpleNamespace(
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "99999999999"},
        close_connection=False,
    )
    monkeypatch.setattr(upload, "j", lambda _handler, _payload, status=200: None)

    upload.handle_upload(handler)

    assert handler.close_connection is True
