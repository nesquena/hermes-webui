"""Focused tests for the extension sidecar proxy contract."""

from types import SimpleNamespace
import io
import json
import urllib.request

import pytest


class FakeHandler:
    def __init__(self, body: bytes = b""):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


@pytest.fixture(autouse=True)
def _clear_extension_env(monkeypatch):
    from api import auth as auth_mod

    for name in (
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
        "HERMES_WEBUI_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    auth_mod._invalidate_password_hash_cache()
    yield
    auth_mod._invalidate_password_hash_cache()


def _use_extension_state_dir(monkeypatch, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "webui-state"
    state_dir.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    import api.extensions as extensions

    monkeypatch.setattr(extensions, "_extension_state_dir", lambda: state_dir)
    return state_dir


def _write_manifest(root, payload):
    (root / "extensions.json").write_text(json.dumps(payload), encoding="utf-8")


def _configure_manifest_extension(monkeypatch, tmp_path, payload):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir(parents=True, exist_ok=True)
    _write_manifest(root, payload)
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")
    return state_dir, root


def test_extension_sidecar_proxy_requires_webui_auth(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api.auth import check_auth

    handler = FakeHandler()
    assert check_auth(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/health", query=""),
    ) is False
    assert handler.status == 401
    assert handler.header("Location") is None


def test_extension_sidecar_proxy_requires_consent_and_reconfirms_after_origin_change(
    tmp_path, monkeypatch
):
    state_dir, root = _configure_manifest_extension(
        monkeypatch,
        tmp_path,
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                    },
                }
            ]
        },
    )

    from api.extensions import (
        ExtensionSidecarProxyError,
        resolve_extension_sidecar_proxy_target,
        set_extension_sidecar_proxy_consent,
    )

    with pytest.raises(ExtensionSidecarProxyError) as unapproved:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping", "debug=1")
    assert unapproved.value.status == 403

    approved = set_extension_sidecar_proxy_consent("templates", True)
    assert approved["sidecars"][0]["proxy"]["consented"] is True
    assert json.loads((state_dir / "extension-overrides.json").read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled_extensions": [],
        "sidecar_proxy_consents": {
            "templates": "http://127.0.0.1:17787",
        },
    }

    target = resolve_extension_sidecar_proxy_target("templates", "v1/ping", "debug=1")
    assert target == {
        "extension_id": "templates",
        "origin": "http://127.0.0.1:17787",
        "proxy_path": "/api/extensions/templates/sidecar/",
        "upstream_url": "http://127.0.0.1:17787/v1/ping?debug=1",
        "proxy_auth": "legacy",
        "auth_token": None,
    }

    encoded_target = resolve_extension_sidecar_proxy_target("templates", "v1%2Fprivate")
    assert encoded_target["upstream_url"] == "http://127.0.0.1:17787/v1%2Fprivate"

    _write_manifest(
        root,
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17788",
                    },
                }
            ]
        },
    )
    changed = set_extension_sidecar_proxy_consent("templates", False)
    assert changed["sidecars"][0]["proxy"]["origin_changed"] is False
    with pytest.raises(ExtensionSidecarProxyError) as changed_origin:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert changed_origin.value.status == 403


def test_extension_sidecar_proxy_rejects_unavailable_surfaces(tmp_path, monkeypatch):
    from api.extensions import ExtensionSidecarProxyError, resolve_extension_sidecar_proxy_target

    _configure_manifest_extension(
        monkeypatch,
        tmp_path / "duplicate",
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                    },
                },
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17788",
                    },
                },
            ]
        },
    )
    with pytest.raises(ExtensionSidecarProxyError) as duplicate:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert duplicate.value.status == 409

    _configure_manifest_extension(
        monkeypatch,
        tmp_path / "manifest_disabled",
        {
            "extensions": [
                {
                    "id": "templates",
                    "enabled": False,
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                    },
                }
            ]
        },
    )
    with pytest.raises(ExtensionSidecarProxyError) as manifest_disabled:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert manifest_disabled.value.status == 409

    state_dir, _root = _configure_manifest_extension(
        monkeypatch,
        tmp_path / "user_disabled",
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                    },
                }
            ]
        },
    )
    (state_dir / "extension-overrides.json").write_text(
        json.dumps(
            {
                "version": 1,
                "disabled_extensions": ["templates"],
                "sidecar_proxy_consents": {
                    "templates": "http://127.0.0.1:17787",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtensionSidecarProxyError) as user_disabled:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert user_disabled.value.status == 409

    _configure_manifest_extension(
        monkeypatch,
        tmp_path / "unsupported",
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "unix-socket",
                        "origin": "http://127.0.0.1:17787",
                    },
                }
            ]
        },
    )
    with pytest.raises(ExtensionSidecarProxyError) as unsupported:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert unsupported.value.status == 409


def test_extension_sidecar_proxy_malformed_consents_fail_closed(tmp_path, monkeypatch):
    from api.extensions import ExtensionSidecarProxyError, resolve_extension_sidecar_proxy_target

    state_dir, _root = _configure_manifest_extension(
        monkeypatch,
        tmp_path / "malformed_consents",
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                    },
                }
            ]
        },
    )
    (state_dir / "extension-overrides.json").write_text(
        json.dumps(
            {
                "version": 1,
                "disabled_extensions": [],
                "sidecar_proxy_consents": {
                    "templates": "http://127.0.0.1:17787",
                    "../bad": "http://127.0.0.1:17788",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionSidecarProxyError) as exc:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert exc.value.status == 403


def test_extension_sidecar_proxy_route_uses_shared_resolver_and_strips_headers(monkeypatch):
    from api import routes

    captured = {}

    class FakeResponse:
        def __init__(self):
            self.status = 202
            self.headers = {
                "Content-Type": "application/json",
                "Set-Cookie": "sidecar=1",
                "Connection": "close, X-Upstream-Hop",
                "X-Sidecar": "ok",
                "X-Upstream-Hop": "strip-me",
            }

        def read(self, *_args):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["data"] = request.data
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": f"http://127.0.0.1:17787/{proxy_path}?{query}",
        },
    )
    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    raw_body = b'{"ping":"pong"}'
    handler = FakeHandler(raw_body)
    handler.headers = {
        "Accept": "application/json",
        "If-None-Match": '"abc123"',
        "Range": "bytes=0-64",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw_body)),
        "Cookie": "webui=secret",
        "Authorization": "Bearer secret",
        "Host": "webui.local",
        "Origin": "http://webui.local",
        "Referer": "http://webui.local/settings",
        "X-CSRF-Token": "secret",
        "X-Sidecar-Auth": "local-token",
        "Connection": "keep-alive, X-Client-Hop",
        "X-Client-Hop": "strip-me",
    }

    result = routes.handle_post(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query="debug=1"),
    )
    assert result is True
    assert captured == {
        "url": "http://127.0.0.1:17787/v1/ping?debug=1",
        "method": "POST",
        "data": raw_body,
        "headers": {
            "accept": "application/json",
            "content-type": "application/json",
            "if-none-match": '"abc123"',
            "range": "bytes=0-64",
            "x-sidecar-auth": "local-token",
        },
        "timeout": 10,
    }
    assert handler.status == 202
    assert handler.body == b'{"ok":true}'
    assert handler.header("Content-Type") == "application/json"
    assert handler.header("X-Sidecar") == "ok"
    assert handler.header("Set-Cookie") is None
    assert handler.header("Connection") is None
    assert handler.header("X-Upstream-Hop") is None


def test_extension_sidecar_proxy_get_rejects_cross_site_browser_request(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "https://evil.example",
        "Host": "webui.local",
        "Sec-Fetch-Site": "cross-site",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 403
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Cross-origin mismatch - check reverse proxy headers"
    }


def test_extension_sidecar_proxy_get_requires_browser_provenance(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    handler = FakeHandler()
    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 403
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Cross-origin mismatch - check reverse proxy headers"
    }


def test_extension_sidecar_proxy_post_requires_browser_provenance(monkeypatch):
    """Regression (#5228 gate): unsafe methods must require the same browser
    provenance as GET. Before the fix only GET enforced require_provenance, so a
    headerless (non-browser) POST fell through the CSRF compatibility path that
    intentionally admits Origin/Referer-less clients — giving POST/PATCH/PUT/
    DELETE weaker provenance than GET on the loopback proxy route."""
    from api import routes

    # If provenance were NOT enforced, the resolver/opener would be reached; make
    # them explode so any regression that lets a headerless POST through fails
    # loudly instead of silently proxying.
    def _boom(*_args, **_kwargs):
        raise AssertionError("headerless POST must be rejected before proxying")

    monkeypatch.setattr("api.extensions.resolve_extension_sidecar_proxy_target", _boom)
    monkeypatch.setattr(
        routes, "_extension_sidecar_proxy_same_origin_opener", _boom
    )

    handler = FakeHandler(b'{"ping":"pong"}')
    # No Origin / Referer / Sec-Fetch-Site — a non-browser client.
    handler.headers = {"Content-Type": "application/json", "Content-Length": "15"}

    result = routes.handle_post(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 403
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Cross-origin mismatch - check reverse proxy headers"
    }


def test_extension_sidecar_proxy_get_allows_same_origin_browser_request_without_csrf_token(monkeypatch):
    from api import routes

    class FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self, *_args):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            return FakeResponse()

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )
    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is True
    assert handler.status == 200
    assert json.loads(handler.body.decode("utf-8")) == {"ok": True}


def test_extension_sidecar_proxy_route_preserves_upstream_http_errors(monkeypatch):
    from api import routes
    from urllib.error import HTTPError

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    class ErrorHeaders(dict):
        pass

    error = HTTPError(
        "http://127.0.0.1:17787/v1/ping",
        418,
        "teapot",
        ErrorHeaders({"Content-Type": "text/plain", "Set-Cookie": "drop=1"}),
        io.BytesIO(b"sidecar said no"),
    )

    class FakeOpener:
        def open(self, request, timeout=10):
            raise error

    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }
    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is True
    assert handler.status == 418
    assert handler.body == b"sidecar said no"
    assert handler.header("Content-Type") == "text/plain"
    assert handler.header("Set-Cookie") is None


def test_extension_sidecar_proxy_get_rejects_sec_fetch_same_origin_without_origin(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    handler = FakeHandler()
    handler.headers = {
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 403
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Cross-origin mismatch - check reverse proxy headers"
    }


def test_extension_sidecar_proxy_get_allows_top_level_navigation_provenance(monkeypatch):
    from api import routes

    class FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self, *_args):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            return FakeResponse()

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )
    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Host": "webui.local",
        "Sec-Fetch-Site": "none",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is True
    assert handler.status == 200
    assert json.loads(handler.body.decode("utf-8")) == {"ok": True}


def test_extension_sidecar_proxy_route_rejects_oversized_upstream_response(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    class FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/octet-stream"}

        def read(self, size=-1):
            assert size == routes._EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES + 1
            return b"x" * size

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            return FakeResponse()

    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 502
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Extension sidecar response too large"
    }


def test_extension_sidecar_proxy_route_rejects_oversized_upstream_http_error(monkeypatch):
    from api import routes
    from urllib.error import HTTPError

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )

    class OversizedBody(io.BytesIO):
        def read(self, size=-1):
            assert size == routes._EXTENSION_SIDECAR_PROXY_MAX_RESPONSE_BYTES + 1
            return b"x" * size

    error = HTTPError(
        "http://127.0.0.1:17787/v1/ping",
        502,
        "bad gateway",
        {},
        OversizedBody(),
    )

    class FakeOpener:
        def open(self, request, timeout=10):
            raise error

    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }

    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 502
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Extension sidecar response too large"
    }


def test_extension_sidecar_proxy_route_returns_sanitized_502(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )
    class FakeOpener:
        def open(self, request, timeout=10):
            raise OSError("no route")

    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: FakeOpener(),
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }
    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is None
    assert handler.status == 502
    assert json.loads(handler.body.decode("utf-8")) == {
        "error": "Failed to reach extension sidecar"
    }


def test_extension_sidecar_proxy_redirect_guard_preserves_origin_only():
    from api import routes

    assert routes._extension_sidecar_proxy_redirect_url(
        "http://127.0.0.1:17787",
        "http://127.0.0.1:17787/v1/ping",
        "/v1/next?debug=1",
    ) == "http://127.0.0.1:17787/v1/next?debug=1"
    assert routes._extension_sidecar_proxy_redirect_url(
        "http://127.0.0.1:17787",
        "http://127.0.0.1:17787/v1/ping",
        "http://evil.example/steal",
    ) is None
    assert routes._extension_sidecar_proxy_redirect_url(
        "http://127.0.0.1:17787",
        "http://127.0.0.1:17787/v1/ping",
        "http://127.0.0.1:17788/other-port",
    ) is None
    assert routes._extension_sidecar_proxy_redirect_url(
        "http://localhost",
        "http://localhost/v1/ping",
        "http://LOCALHOST:80/v1/next",
    ) == "http://LOCALHOST:80/v1/next"
    assert routes._extension_sidecar_proxy_redirect_url(
        "https://localhost:443",
        "https://localhost/v1/ping",
        "https://localhost/v1/next",
    ) == "https://localhost/v1/next"


def test_extension_sidecar_proxy_route_uses_same_origin_redirect_opener(monkeypatch):
    from api import routes

    captured = {}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, *_args):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
        },
    )
    monkeypatch.setattr(
        routes,
        "_extension_sidecar_proxy_same_origin_opener",
        lambda allowed_origin: (
            captured.__setitem__("allowed_origin", allowed_origin),
            FakeOpener(),
        )[1],
    )

    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://webui.local",
        "Host": "webui.local",
        "Sec-Fetch-Site": "same-origin",
    }
    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is True
    assert captured == {
        "allowed_origin": "http://127.0.0.1:17787",
        "url": "http://127.0.0.1:17787/v1/ping",
        "timeout": 10,
    }
    assert handler.status == 200
    assert handler.body == b'{"ok":true}'


def test_extension_sidecar_proxy_opener_disables_ambient_proxies(monkeypatch):
    from api import routes

    captured = {}

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return object()

    monkeypatch.setattr(routes, "build_opener", fake_build_opener)
    opener = routes._extension_sidecar_proxy_same_origin_opener("http://127.0.0.1:17787")
    assert opener is not None
    proxy_handlers = [
        handler
        for handler in captured["handlers"]
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_extension_sidecar_proxy_consent_route_is_wired(monkeypatch):
    from api import routes

    captured = {}

    def fake_j(handler, data, status=200, headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"id": "templates", "approved": True})
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        "api.extensions.set_extension_sidecar_proxy_consent",
        lambda extension_id, approved: {
            "ok": True,
            "id": extension_id,
            "approved": approved,
        },
    )
    handler = FakeHandler()

    assert routes.handle_post(
        handler,
        SimpleNamespace(path="/api/extensions/sidecar-proxy-consent"),
    ) is True
    assert captured == {
        "status": 200,
        "data": {"ok": True, "id": "templates", "approved": True},
    }


def test_extension_sidecar_proxy_consent_route_fails_closed_auth_off(tmp_path, monkeypatch):
    # Frank #6331 blocker 1 (route-level regression): with WebUI auth OFF, driving
    # the REAL consent function through the HTTP route for a token-v1 sidecar must
    # return 403 (fail closed), not persist consent. Uses the real
    # set_extension_sidecar_proxy_consent (NOT a mock) so the route + auth gate are
    # exercised end-to-end.
    from api import routes

    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
    from api.auth import _invalidate_password_hash_cache, is_auth_enabled
    _invalidate_password_hash_cache()
    assert is_auth_enabled() is False
    _token_v1_manifest(monkeypatch, tmp_path, origin="http://127.0.0.1:17787")

    captured = {}

    def fake_bad(handler, msg, status=400):
        captured["status"] = status
        captured["msg"] = msg
        return True

    def fake_j(handler, data, status=200, headers=None):
        captured["status"] = status
        captured["data"] = data
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"id": "templates", "approved": True})
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    handler = FakeHandler()

    assert routes.handle_post(
        handler,
        SimpleNamespace(path="/api/extensions/sidecar-proxy-consent"),
    ) is True
    # Must be a fail-closed 403 from the real consent function, not a 200 grant.
    assert captured.get("status") == 403
    assert "data" not in captured
    # And no consent must have been persisted.
    from api.extensions import resolve_extension_sidecar_proxy_target, ExtensionSidecarProxyError
    with pytest.raises(ExtensionSidecarProxyError) as exc:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert exc.value.status == 403


# ── token-v1 proxy auth (§9.1/§9.2) ─────────────────────────────────────────

def _token_v1_manifest(monkeypatch, tmp_path, origin="http://127.0.0.1:17787"):
    return _configure_manifest_extension(
        monkeypatch,
        tmp_path,
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": origin,
                        "proxy_auth": "token-v1",
                    },
                }
            ]
        },
    )


def test_token_v1_injects_persisted_token_when_auth_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "pw")
    from api.auth import _invalidate_password_hash_cache
    _invalidate_password_hash_cache()
    _token_v1_manifest(monkeypatch, tmp_path)
    from api.extensions import (
        resolve_extension_sidecar_proxy_target,
        set_extension_sidecar_proxy_consent,
    )
    import api.extension_sidecar_auth as sc

    set_extension_sidecar_proxy_consent("templates", True)
    target = resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert target["proxy_auth"] == "token-v1"
    assert target["auth_token"] and len(target["auth_token"]) > 30
    assert target["auth_token"] == sc.current_token("templates")


def test_token_v1_auth_off_blocks_consent_and_resolution_fail_closed(tmp_path, monkeypatch):
    # Frank #6331 blocker 1: with WebUI auth OFF, token-v1 must fail closed at BOTH
    # the consent grant and the resolution path — regardless of a loopback origin.
    # A loopback sidecar is NOT a sufficient guard: any caller that can reach the
    # (unauthenticated) WebUI listener could otherwise self-grant consent and drive
    # the token-bearing proxy (a forwarding oracle); another local UID can reach the
    # listener without ever reading the 0600 token file.
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
    from api.auth import _invalidate_password_hash_cache, is_auth_enabled
    _invalidate_password_hash_cache()
    assert is_auth_enabled() is False
    _token_v1_manifest(monkeypatch, tmp_path, origin="http://127.0.0.1:17787")
    from api.extensions import (
        ExtensionSidecarProxyError,
        set_extension_sidecar_proxy_consent,
    )

    # Consent grant is rejected fail-closed (403) even for a loopback origin.
    with pytest.raises(ExtensionSidecarProxyError) as consent_exc:
        set_extension_sidecar_proxy_consent("templates", True)
    assert consent_exc.value.status == 403


def test_token_v1_resolution_fails_closed_when_auth_disabled_after_consent(tmp_path, monkeypatch):
    # Defense in depth: even if a consent record somehow exists (e.g. granted while
    # auth was enabled, then auth turned off), the resolution path must ALSO fail
    # closed so a stale consent can't be exercised without WebUI auth.
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "pw")
    from api.auth import _invalidate_password_hash_cache, is_auth_enabled
    _invalidate_password_hash_cache()
    _token_v1_manifest(monkeypatch, tmp_path, origin="http://127.0.0.1:17787")
    from api.extensions import (
        ExtensionSidecarProxyError,
        resolve_extension_sidecar_proxy_target,
        set_extension_sidecar_proxy_consent,
    )

    # Grant consent WHILE auth is enabled (allowed).
    assert is_auth_enabled() is True
    set_extension_sidecar_proxy_consent("templates", True)
    # Now disable auth and confirm resolution refuses the pre-existing consent.
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
    _invalidate_password_hash_cache()
    assert is_auth_enabled() is False
    with pytest.raises(ExtensionSidecarProxyError) as exc:
        resolve_extension_sidecar_proxy_target("templates", "v1/ping")
    assert exc.value.status == 403


def test_unknown_proxy_auth_value_rejects_sidecar(tmp_path, monkeypatch):
    _configure_manifest_extension(
        monkeypatch,
        tmp_path,
        {
            "extensions": [
                {
                    "id": "templates",
                    "sidecar": {
                        "type": "loopback",
                        "origin": "http://127.0.0.1:17787",
                        "proxy_auth": "totally-bogus",
                    },
                }
            ]
        },
    )
    from api.extensions import (
        ExtensionSidecarProxyError,
        set_extension_sidecar_proxy_consent,
    )

    # Unknown proxy_auth -> _sidecar_from_manifest_entry rejects the record, so the
    # sidecar never becomes available/consentable (fail closed, not silent-open).
    # The rejection surfaces at consent time (the earliest resolve path).
    with pytest.raises(ExtensionSidecarProxyError):
        set_extension_sidecar_proxy_consent("templates", True)


def test_status_payload_flags_local_unprotected_when_auth_off(tmp_path, monkeypatch):
    _token_v1_manifest(monkeypatch, tmp_path)
    from api.extensions import get_extension_status

    status = get_extension_status()
    sidecars = status.get("sidecars") or []
    tmpl = next((s for s in sidecars if s.get("id") == "templates"), None)
    assert tmpl is not None
    proxy = tmpl["proxy"]
    assert proxy["proxy_auth"] == "token-v1"
    assert proxy["posture"] == "local_unprotected"  # auth off -> panel warns pre-consent


def test_inbound_x_hermes_header_is_stripped(monkeypatch):
    from api.routes import _extension_sidecar_proxy_request_headers

    class H:
        headers = {
            "X-Hermes-Sidecar-Token": "forged",
            "X-Custom": "ok",
            "Cookie": "secret",
        }

    out = _extension_sidecar_proxy_request_headers(H())
    assert "X-Custom" in out
    assert not any(k.lower().startswith("x-hermes-") for k in out)
    assert not any(k.lower() == "cookie" for k in out)


def test_token_module_persists_and_rotates(tmp_path, monkeypatch):
    import api.extension_sidecar_auth as sc

    # Patch the dynamic dir resolver (mirrors how _extension_state_dir is patched
    # elsewhere) so the token lands in an isolated tmp dir, not the shared session
    # state dir.
    token_dir = tmp_path / "sidecar-auth"
    monkeypatch.setattr(sc, "_token_dir", lambda: token_dir)
    sc._CACHE.clear()
    tok = sc.ensure_token("templates")
    assert tok and sc.current_token("templates") == tok
    assert (token_dir / "templates.token").exists()
    assert sc.current_token("never") is None            # fail closed
    assert sc.ensure_token("../evil") is None            # path-escape rejected
    # IDs are filename components. Reject rather than silently trim whitespace so
    # validation and _token_path() always operate on the exact same string.
    for invalid_id in ("templates\n", " templates", "templates "):
        assert sc.ensure_token(invalid_id) is None
        assert not (token_dir / f"{invalid_id}.token").exists()
    # canonical (wider) id grammar is honored: uppercase/dot ids work
    assert sc.ensure_token("RSS.Feeds") is not None
    new = sc.reset_token("templates")
    assert new and new != tok and sc.current_token("templates") == new
    sc._CACHE.clear()


def test_token_v1_route_injects_token_and_strips_response(monkeypatch):
    # The 5 most load-bearing lines: the injected token must reach the upstream
    # Request, and any x-hermes-* on the response must be stripped before it
    # reaches the browser. (Fable coreA item 3.)
    from api import routes

    captured = {}

    class FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {
                "Content-Type": "application/json",
                "X-Hermes-Echo": "leak-me",  # must be stripped from client response
            }

        def read(self, *_a):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeOpener:
        def open(self, request, timeout=10):
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        "api.extensions.resolve_extension_sidecar_proxy_target",
        lambda extension_id, proxy_path, query="": {
            "extension_id": extension_id,
            "origin": "http://127.0.0.1:17787",
            "proxy_path": "/api/extensions/templates/sidecar/",
            "upstream_url": "http://127.0.0.1:17787/v1/ping",
            "proxy_auth": "token-v1",
            "auth_token": "injected-token-abc",
        },
    )
    monkeypatch.setattr(
        routes, "_extension_sidecar_proxy_same_origin_opener", lambda o: FakeOpener()
    )

    handler = FakeHandler()
    handler.headers = {
        "Accept": "application/json",
        "Host": "webui.local",
        "Origin": "http://webui.local",
        "Referer": "http://webui.local/",
    }
    result = routes.handle_get(
        handler,
        SimpleNamespace(path="/api/extensions/templates/sidecar/v1/ping", query=""),
    )
    assert result is True
    # token injected on the way to the sidecar
    assert captured["headers"].get("x-hermes-sidecar-token") == "injected-token-abc"
    # token/x-hermes header stripped on the way back to the browser
    assert handler.header("X-Hermes-Echo") is None
