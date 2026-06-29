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
