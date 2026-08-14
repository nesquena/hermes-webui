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


# ── Header serialization: exactly one Connection: close, never a crash ────────
#
# The advertisement lives in Handler.end_headers() via
# api.helpers.advertise_connection_close(). Two things went wrong there and
# both are pinned below:
#
#   1. The first cut keyed dedup off a private `_close_advertised` flag it set
#      itself. Every SSE endpoint already sends its own `Connection: close`
#      (which BaseHTTPRequestHandler.send_header turns into
#      close_connection=True), so end_headers() appended a SECOND copy to every
#      stream response.
#   2. It read `self.close_connection` unguarded. That attribute only exists
#      once a request line has been parsed, so the many suite handlers built
#      with `Handler.__new__(Handler)` raised AttributeError instead of
#      emitting headers.


def _render_response_headers(prepare=None) -> str:
    """Drive the production Handler.end_headers() into a byte buffer."""
    import io

    from server import Handler

    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.close_connection = False
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    if prepare is not None:
        prepare(handler)
    Handler.end_headers(handler)
    return handler.wfile.getvalue().decode("latin-1")


def test_armed_close_is_advertised_exactly_once():
    """A reject-before-read must TELL the client the socket is gone."""
    from api.helpers import arm_connection_close

    rendered = _render_response_headers(arm_connection_close)

    assert rendered.count("Connection: close") == 1, rendered


def test_sse_style_explicit_close_is_not_doubled():
    """SSE sends its own Connection: close; end_headers() must not repeat it.

    send_header('Connection', 'close') flips close_connection inside the
    stdlib, so a self-keyed dedup flag would emit the header twice here.
    """

    def prepare(handler):
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Connection", "close")

    rendered = _render_response_headers(prepare)

    assert rendered.count("Connection: close") == 1, rendered


def test_keep_alive_response_advertises_no_close():
    """An ordinary response must keep its keep-alive."""
    rendered = _render_response_headers(
        lambda handler: handler.send_header("Content-Type", "application/json")
    )

    assert "Connection: close" not in rendered, rendered


def test_end_headers_on_handler_stub_without_close_connection(monkeypatch):
    """A partially-built handler stub has no close_connection — don't raise.

    Much of the suite builds handlers with `Handler.__new__(Handler)` and never
    parses a request, so the attribute is simply absent. Reading it unguarded
    turned every such test into an AttributeError.
    """
    from http.server import BaseHTTPRequestHandler

    from server import Handler

    sent: list[tuple[str, str]] = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    assert not hasattr(handler, "close_connection")
    Handler.end_headers(handler)  # must not raise

    assert "Content-Security-Policy-Report-Only" in dict(sent)
    assert "Connection" not in dict(sent), "nothing was armed, so nothing to advertise"


def test_check_auth_or_close_on_handler_stub_without_close_connection(monkeypatch):
    """check_auth_or_close must not invent close_connection on a stub.

    Arming a handler that never had the attribute would leave a bogus
    keep-alive verdict behind, and reading it unguarded raised AttributeError
    (which _handle_write then converted into a spurious 500).
    """
    import api.auth as auth
    from server import Handler

    handler = Handler.__new__(Handler)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: True)

    assert auth.check_auth_or_close(handler, SimpleNamespace(path="/api/x")) is True
    assert not hasattr(handler, "close_connection")


def test_check_auth_or_close_restores_keep_alive_on_success(monkeypatch):
    """A successful auth must not inherit the armed close."""
    import api.auth as auth

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: True)

    assert auth.check_auth_or_close(cast(Any, handler), SimpleNamespace(path="/api/x")) is True
    assert handler.close_connection is False


def test_arm_connection_close_tolerates_a_read_only_handler():
    """Arming must never turn a 4xx into a 500, even on an odd handler."""
    from api.helpers import arm_connection_close

    class Frozen:
        __slots__ = ()

    arm_connection_close(Frozen())  # must not raise
