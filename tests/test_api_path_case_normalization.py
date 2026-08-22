"""
Regression tests for case-insensitive /api/ route matching (#6589).

The server case-folds ``/api/*`` paths ONLY for route matching
(``server._fold_api_path``) and retains the original-case path on the handler,
so case-sensitive dynamic values carried as path segments -- share tokens, MCP
server names -- survive normalization instead of being corrupted.

Covers the review's four cases:
  1. mixed-case fixed route dispatch (``/api/Auth/Status`` -> ``/api/auth/status``)
  2. uppercase ``/API/`` prefix dispatch (``/API/Auth/Status`` -> ``/api/auth/status``)
  3. a share token containing uppercase characters is preserved (no 404)
  4. an MCP server name containing uppercase characters is preserved
     (PATCH / DELETE / PUT)
Plus explicit guards that existing lowercase routes are unchanged.

Note on the ``is not False`` assertions: handlers that respond via ``j()``
return ``None`` (``j`` has no return value), and server.py treats anything
``is not False`` as handled (``False`` is reserved for "no route matched").
"""

import io
import json
from unittest.mock import patch
from urllib.parse import urlparse

from api.routes import handle_delete, handle_get, handle_patch, handle_put
from server import _fold_api_path


class _Headers(dict):
    """Minimal dict mirroring BaseHTTPRequestHandler.headers (``.get`` default)."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


class _FakeHandler:
    """Minimal stand-in for the server Handler (tests/test_compress_status_404_fix.py)."""

    def __init__(self, body=b"{}"):
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO(body)
        self.status = None
        self.sent_headers = {}
        self.headers = _Headers({"Content-Length": str(len(body))})
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _dispatch(path, body=b"{}"):
    """Fold the request path exactly like server.do_GET / server._handle_write."""
    handler = _FakeHandler(body=body)
    parsed = _fold_api_path(handler, urlparse("http://example.com" + path))
    return handler, parsed


# -- 1/2. mixed-case fixed-route + uppercase /API/ prefix dispatch -----------


def test_mixed_case_fixed_route_dispatches_to_lowercase_route():
    handler, parsed = _dispatch("/api/Auth/Status")
    assert handle_get(handler, parsed) is not False
    assert handler.status == 200
    assert "auth_enabled" in handler.payload()


def test_uppercase_api_prefix_dispatches():
    handler, parsed = _dispatch("/API/Auth/Status")
    assert handle_get(handler, parsed) is not False
    assert handler.status == 200
    assert "auth_enabled" in handler.payload()


def test_lowercase_route_is_unchanged():
    handler, parsed = _dispatch("/api/auth/status")
    assert handle_get(handler, parsed) is not False
    assert handler.status == 200
    assert "auth_enabled" in handler.payload()


# -- 3. share token with uppercase characters survives ------------------------


def test_share_token_with_uppercase_characters_is_preserved():
    handler, parsed = _dispatch("/api/Share/AbC123XyZ")
    calls = []

    def fake_load_share(token):
        calls.append(token)
        return {"token": token, "title": "case-sensitive-token"}

    with patch("api.routes.load_share", fake_load_share):
        result = handle_get(handler, parsed)

    assert result is not False
    assert handler.status == 200
    assert calls == ["AbC123XyZ"], f"share token corrupted: {calls!r}"
    assert handler.payload()["share"]["token"] == "AbC123XyZ"


def test_share_token_lowercase_route_still_works():
    handler, parsed = _dispatch("/api/share/plain-token-abc")
    calls = []

    def fake_load_share(token):
        calls.append(token)
        return {"token": token, "title": "lowercase"}

    with patch("api.routes.load_share", fake_load_share):
        result = handle_get(handler, parsed)

    assert result is not False
    assert calls == ["plain-token-abc"]


# -- 4. MCP server name with uppercase characters survives (PATCH/DELETE/PUT) -


def test_mcp_server_name_preserved_on_patch():
    handler, parsed = _dispatch("/api/Mcp/Servers/MyServer")
    captured = {}

    def fake_toggle(h, name, body):
        captured["name"] = name
        return True

    with patch("api.routes._handle_mcp_server_toggle", fake_toggle):
        result = handle_patch(handler, parsed)

    assert result is True
    assert captured.get("name") == "MyServer", f"name corrupted: {captured!r}"


def test_mcp_server_name_preserved_on_delete():
    handler, parsed = _dispatch("/api/Mcp/Servers/MyServer")
    captured = {}

    def fake_delete(h, name):
        captured["name"] = name
        return True

    with patch("api.routes._handle_mcp_server_delete", fake_delete):
        result = handle_delete(handler, parsed)

    assert result is True
    assert captured.get("name") == "MyServer", f"name corrupted: {captured!r}"


def test_mcp_server_name_preserved_on_put():
    handler, parsed = _dispatch("/api/Mcp/Servers/MyServer")
    captured = {}

    def fake_update(h, name, body):
        captured["name"] = name
        return True

    with patch("api.routes._handle_mcp_server_update", fake_update):
        result = handle_put(handler, parsed)

    assert result is True
    assert captured.get("name") == "MyServer", f"name corrupted: {captured!r}"


# -- sidecar proxy captures (case-sensitive proxy path) -----------------------


def test_sidecar_proxy_path_capture_preserved():
    """/api/extensions/*/sidecar/* carries a case-sensitive proxy path."""
    from api.routes import _handle_extension_sidecar_proxy

    handler, parsed = _dispatch("/api/Extensions/Ext_A/sidecar/Path/With/Case")

    # Fake same-origin provenance so the proxy handler proceeds past CSRF.
    handler.headers["Origin"] = "http://example.com"
    handler.headers["Host"] = "example.com"

    class _FakeResponse:
        status = 200
        headers = {}

        def read(self, n=-1):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeOpener:
        def open(self, request, timeout=None):
            return _FakeResponse()

    with patch("api.extensions.resolve_extension_sidecar_proxy_target") as resolve:
        resolve.return_value = {
            "upstream_url": "http://127.0.0.1:9999",
            "origin": "http://example.com",
            "auth_token": None,
        }
        with patch("api.routes.Request") as fake_request, patch(
            "api.routes._extension_sidecar_proxy_same_origin_opener",
            return_value=_FakeOpener(),
        ):
            result = _handle_extension_sidecar_proxy(handler, parsed, "GET")

    assert result is True
    assert resolve.call_count == 1
    args = resolve.call_args.args
    kwargs = resolve.call_args.kwargs
    assert args and args[0] == "Ext_A", f"extension id corrupted: {args!r}"
    assert args and args[1] == "Path/With/Case", f"proxy path corrupted: {args!r}"
    assert kwargs.get("query") == ""
