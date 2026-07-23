"""Capability-separation tests for wallpaper metadata and generic settings."""

import copy
import io
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import zlib

import pytest


_VALID_WALLPAPER_FILE = f"wallpaper-{'a' * 64}.png"
_WALLPAPER_STATE = {
    "wallpaper_file": _VALID_WALLPAPER_FILE,
    "wallpaper_opacity": 0.45,
    "wallpaper_scope": "app",
}


class _FakeHandler:
    def __init__(self, body: object | None = None):
        raw = json.dumps({} if body is None else body).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.request = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path: Path):
    import api.config as config
    from api.auth import _invalidate_password_hash_cache

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
    _invalidate_password_hash_cache()
    yield config, settings_file
    _invalidate_password_hash_cache()


def _post_settings(body: object) -> _FakeHandler:
    from api.routes import handle_post

    handler = _FakeHandler(body)
    handle_post(handler, urlparse("http://example.com/api/settings"))
    return handler


def _get_settings() -> _FakeHandler:
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urlparse("http://example.com/api/settings"))
    return handler


def _seed_settings(config, settings_file: Path) -> bytes:
    config.save_settings(
        {
            "theme": "dark",
            "skin": "default",
            "font_size": "default",
            "bot_name": "Hermes",
            "show_token_usage": False,
        }
    )
    with config._SETTINGS_WRITE_LOCK:
        config._save_wallpaper_settings_locked(_WALLPAPER_STATE)
    return settings_file.read_bytes()


def test_internal_capability_persists_valid_wallpaper_state(isolated_settings) -> None:
    config, settings_file = isolated_settings

    with config._SETTINGS_WRITE_LOCK:
        saved = config._save_wallpaper_settings_locked(_WALLPAPER_STATE)

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {key: saved[key] for key in config.WALLPAPER_SETTINGS_KEYS} == _WALLPAPER_STATE
    assert {key: persisted[key] for key in config.WALLPAPER_SETTINGS_KEYS} == _WALLPAPER_STATE


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"wallpaper_file": "../../outside.png"},
        {"wallpaper_opacity": True},
        {"wallpaper_opacity": 1.01},
        {"wallpaper_scope": "desktop"},
    ],
)
def test_internal_capability_rejects_invalid_wallpaper_state_without_writing(
    isolated_settings, invalid_update
) -> None:
    config, settings_file = isolated_settings
    before = _seed_settings(config, settings_file)

    with config._SETTINGS_WRITE_LOCK:
        with pytest.raises((TypeError, ValueError)):
            config._save_wallpaper_settings_locked(invalid_update)

    assert settings_file.read_bytes() == before


@pytest.mark.parametrize("wallpaper_key", sorted(_WALLPAPER_STATE))
def test_generic_settings_rejects_every_wallpaper_key_without_writing(
    isolated_settings, wallpaper_key
) -> None:
    config, settings_file = isolated_settings
    before = _seed_settings(config, settings_file)

    handler = _post_settings({wallpaper_key: _WALLPAPER_STATE[wallpaper_key]})

    assert handler.status == 400
    assert "wallpaper" in handler.json_body()["error"].lower()
    assert settings_file.read_bytes() == before


def test_generic_settings_preserves_malformed_raw_wallpaper_state(
    isolated_settings,
) -> None:
    config, settings_file = isolated_settings
    malformed_wallpaper = {
        "wallpaper_file": "../../outside.png",
        "wallpaper_opacity": 2,
        "wallpaper_scope": "desktop",
    }
    settings_file.write_text(
        json.dumps(
            {
                "theme": "dark",
                "show_token_usage": False,
                **malformed_wallpaper,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    handler = _post_settings({"show_token_usage": True})

    assert handler.status == 200
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {
        key: persisted[key] for key in config.WALLPAPER_SETTINGS_KEYS
    } == malformed_wallpaper
    assert persisted["show_token_usage"] is True
    assert config.WALLPAPER_SETTINGS_KEYS.isdisjoint(handler.json_body())
    assert {
        key: config.load_settings()[key] for key in config.WALLPAPER_SETTINGS_KEYS
    } == {
        "wallpaper_file": "",
        "wallpaper_opacity": 0.8,
        "wallpaper_scope": "chat",
    }


def test_generic_settings_get_and_save_responses_share_public_projection(
    isolated_settings,
) -> None:
    config, settings_file = isolated_settings
    _seed_settings(config, settings_file)

    get_handler = _get_settings()
    save_handler = _post_settings({"theme": "light", "skin": "default"})

    assert get_handler.status == 200
    assert save_handler.status == 200
    for response in (get_handler.json_body(), save_handler.json_body()):
        assert "password_hash" not in response
        assert config.WALLPAPER_SETTINGS_KEYS.isdisjoint(response)

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {key: persisted[key] for key in config.WALLPAPER_SETTINGS_KEYS} == _WALLPAPER_STATE
    assert persisted["theme"] == "light"


@pytest.mark.parametrize(
    "mixed_payload",
    [
        {
            "wallpaper_opacity": 0.2,
            "_set_password": "new-secret",
            "bot_name": " Mutated if reached ",
        },
        {
            "wallpaper_scope": "chat",
            "_clear_password": True,
            "_auth_disabled_acknowledged": True,
        },
        {
            "wallpaper_file": "",
            "_passwordless": True,
        },
        {
            "wallpaper_scope": "chat",
            "theme": "light",
            "skin": "charizard",
            "font_size": "large",
        },
    ],
)
def test_mixed_payload_rejects_before_mutation_auth_side_effects_or_persistence(
    isolated_settings, monkeypatch, mixed_payload
) -> None:
    import api.auth as auth
    import api.passkeys as passkeys
    import api.routes as routes

    config, settings_file = isolated_settings
    before = _seed_settings(config, settings_file)
    submitted = copy.deepcopy(mixed_payload)
    original_body = copy.deepcopy(submitted)

    def forbidden_side_effect(*args, **kwargs):
        pytest.fail("wallpaper rejection must precede auth side effects")

    monkeypatch.setattr(routes, "read_body", lambda handler: submitted)
    monkeypatch.setattr(auth, "_hash_password", forbidden_side_effect)
    monkeypatch.setattr(auth, "_invalidate_password_hash_cache", forbidden_side_effect)
    monkeypatch.setattr(auth, "create_session", forbidden_side_effect)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", forbidden_side_effect)
    monkeypatch.setattr(passkeys, "clear_credentials", forbidden_side_effect)

    handler = _FakeHandler()
    routes.handle_post(handler, urlparse("http://example.com/api/settings"))

    assert handler.status == 400
    assert "wallpaper" in handler.json_body()["error"].lower()
    assert submitted == original_body
    assert settings_file.read_bytes() == before


class _TrackedSnapshot:
    def __init__(self, data: bytes = b"wallpaper-bytes"):
        self.file = io.BytesIO(data)
        self.size = len(data)
        self.mime_type = "image/png"
        self.etag = f'"{"b" * 64}"'
        self.close_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close_count += 1
        self.file.close()


class _FailingWriter:
    def write(self, data):
        raise BrokenPipeError("client disconnected")


def _header_values(handler: _FakeHandler, name: str) -> list[str]:
    return [value for key, value in handler.sent_headers if key.lower() == name.lower()]


def _get_wallpaper(path: str, *, headers: dict | None = None) -> _FakeHandler:
    from api.routes import handle_get

    handler = _FakeHandler()
    if headers:
        handler.headers.update(headers)
    handle_get(handler, urlparse(f"http://example.com{path}"))
    return handler


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (
            {
                "has_wallpaper": True,
                "opacity": 0.45,
                "scope": "app",
                "mime_type": "image/png",
                "image_version": "a" * 64,
            },
            {
                "has_wallpaper": True,
                "opacity": 0.45,
                "scope": "app",
                "mime_type": "image/png",
                "image_version": "a" * 64,
            },
        ),
        (
            {
                "has_wallpaper": False,
                "opacity": 0.8,
                "scope": "chat",
                "mime_type": None,
                "image_version": None,
            },
            {
                "has_wallpaper": False,
                "opacity": 0.8,
                "scope": "chat",
                "mime_type": None,
                "image_version": None,
            },
        ),
    ],
)
def test_wallpaper_info_returns_exact_authoritative_schema(monkeypatch, info, expected) -> None:
    import api.wallpaper as wallpaper

    monkeypatch.setattr(
        wallpaper,
        "get_wallpaper_info",
        lambda: wallpaper.WallpaperInfo(**info),
    )

    handler = _get_wallpaper("/api/wallpaper/info")

    assert handler.status == 200
    assert handler.json_body() == expected
    assert set(handler.json_body()) == {
        "has_wallpaper",
        "opacity",
        "scope",
        "mime_type",
        "image_version",
    }
    assert type(handler.json_body()["has_wallpaper"]) is bool
    assert type(handler.json_body()["opacity"]) in {int, float}


def test_wallpaper_image_streams_validated_snapshot_and_closes_once(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    snapshot = _TrackedSnapshot()
    monkeypatch.setattr(wallpaper, "open_wallpaper_snapshot", lambda: snapshot)

    handler = _get_wallpaper("/api/wallpaper/image")

    assert handler.status == 200
    assert bytes(handler.body) == b"wallpaper-bytes"
    assert _header_values(handler, "Content-Type") == ["image/png"]
    assert _header_values(handler, "Content-Length") == [str(len(b"wallpaper-bytes"))]
    assert _header_values(handler, "ETag") == [f'"{"b" * 64}"']
    assert _header_values(handler, "Cache-Control") == ["private, no-cache"]
    assert _header_values(handler, "X-Content-Type-Options") == ["nosniff"]
    assert snapshot.close_count == 1


@pytest.mark.parametrize(
    ("if_none_match", "expected_status"),
    [
        (f'"{"b" * 64}"', 304),
        ("*", 304),
        (f'"other",  "{"b" * 64}"  ', 304),
        (f'W/"{"b" * 64}"', 200),
        ("b" * 64, 200),
        (f'W/"other", "{"b" * 64}"', 304),
    ],
)
def test_wallpaper_image_etag_matching_and_descriptor_close(
    monkeypatch, if_none_match, expected_status
) -> None:
    import api.wallpaper as wallpaper

    snapshot = _TrackedSnapshot()
    monkeypatch.setattr(wallpaper, "open_wallpaper_snapshot", lambda: snapshot)

    handler = _get_wallpaper(
        "/api/wallpaper/image",
        headers={"If-None-Match": if_none_match},
    )

    assert handler.status == expected_status
    assert _header_values(handler, "ETag") == [snapshot.etag]
    assert _header_values(handler, "Cache-Control") == ["private, no-cache"]
    assert _header_values(handler, "X-Content-Type-Options") == ["nosniff"]
    if expected_status == 304:
        assert bytes(handler.body) == b""
        assert _header_values(handler, "Content-Length") == []
    else:
        assert bytes(handler.body) == b"wallpaper-bytes"
    assert snapshot.close_count == 1


def test_wallpaper_image_absence_returns_exact_json_404(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    monkeypatch.setattr(wallpaper, "open_wallpaper_snapshot", lambda: None)

    handler = _get_wallpaper("/api/wallpaper/image")

    assert handler.status == 404
    assert set(handler.json_body()) == {"error"}


@pytest.mark.parametrize("failure_point", ["headers", "write"])
def test_wallpaper_image_descriptor_closes_once_on_response_failure(
    monkeypatch, failure_point
) -> None:
    import api.wallpaper as wallpaper

    snapshot = _TrackedSnapshot()
    monkeypatch.setattr(wallpaper, "open_wallpaper_snapshot", lambda: snapshot)
    handler = _FakeHandler()
    if failure_point == "headers":
        handler.end_headers = lambda: (_ for _ in ()).throw(
            ConnectionResetError("client disconnected")
        )
    else:
        handler.wfile = _FailingWriter()

    with pytest.raises((BrokenPipeError, ConnectionResetError)):
        from api.routes import handle_get

        handle_get(handler, urlparse("http://example.com/api/wallpaper/image"))

    assert snapshot.close_count == 1


class _RawHeaders(dict):
    def __init__(self, values: list[tuple[str, str]]):
        super().__init__()
        self._values = values
        for name, value in values:
            self[name] = value

    def get_all(self, name, default=None):
        matched = [value for key, value in self._values if key.lower() == name.lower()]
        return matched if matched else (default if default is not None else [])


class _RawUploadHandler(_FakeHandler):
    def __init__(self, body: bytes, header_values: list[tuple[str, str]]):
        super().__init__({})
        self.rfile = io.BytesIO(body)
        self.headers = _RawHeaders(header_values)
        self.close_connection = False


def _tiny_png() -> bytes:
    def chunk(name: bytes, payload: bytes = b"") -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload))
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")
    ) + chunk(b"IEND")


def _post_wallpaper(
    query: str,
    *,
    body: bytes | None = None,
    headers: list[tuple[str, str]] | None = None,
) -> _RawUploadHandler:
    from api.routes import handle_post

    data = _tiny_png() if body is None else body
    raw_headers = headers or [
        ("Content-Type", "application/octet-stream"),
        ("Content-Length", str(len(data))),
    ]
    handler = _RawUploadHandler(data, raw_headers)
    handle_post(handler, urlparse(f"http://example.com/api/wallpaper?{query}"))
    return handler


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ([('Content-Length', '1')], 400),
        ([('Content-Type', 'image/png'), ('Content-Length', '1')], 400),
        ([('Content-Type', 'application/octet-stream')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '0')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '+1')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', ' 1')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '1,1')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '1'), ('Content-Length', '1')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '1'), ('Transfer-Encoding', 'chunked')], 400),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', str(10 * 1024 * 1024 + 1))], 413),
        ([('Content-Type', 'application/octet-stream'), ('Content-Length', '9' * 1000)], 413),
    ],
)
def test_wallpaper_upload_grammar_rejects_invalid_headers(headers, expected_status) -> None:
    handler = _post_wallpaper("opacity=0.8&scope=chat", body=b"x", headers=headers)

    assert handler.status == expected_status
    assert set(handler.json_body()) == {"error"}
    assert handler.close_connection is True


@pytest.mark.parametrize(
    "query",
    [
        "",
        "opacity=0.8",
        "scope=chat",
        "opacity=&scope=chat",
        "opacity=0.8&scope=",
        "opacity=0.8&opacity=0.7&scope=chat",
        "opacity=0.8&scope=chat&scope=app",
        "opacity=0.8&scope=chat&unknown=",
        "opacity=0.8;scope=chat",
        "opacity=%ZZ&scope=chat",
        "opacity=0.8&scope=%0Achat",
        "opacity=-0.1&scope=chat",
        "opacity=1.1&scope=chat",
        "opacity=1e0&scope=chat",
        "opacity=true&scope=chat",
        "opacity=NaN&scope=chat",
        "opacity=0.8&scope=CHAT",
        "opacity=0.8&scope=desktop",
    ],
)
def test_wallpaper_upload_query_rejects_noncanonical_grammar(query) -> None:
    handler = _post_wallpaper(query)

    assert handler.status == 400
    assert set(handler.json_body()) == {"error"}
    assert handler.close_connection is True


def test_wallpaper_upload_actual_overflow_at_encoded_limit_returns_413(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    monkeypatch.setattr(
        wallpaper,
        "replace_wallpaper",
        lambda *_args, **_kwargs: pytest.fail("overflow must reject before storage"),
    )
    maximum = 10 * 1024 * 1024
    handler = _post_wallpaper(
        "opacity=0.8&scope=chat",
        body=b"x" * (maximum + 1),
        headers=[
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", str(maximum)),
        ],
    )

    assert handler.status == 413
    assert set(handler.json_body()) == {"error"}


@pytest.mark.parametrize(
    ("query", "expected_opacity", "expected_scope"),
    [
        ("opacity=0&scope=chat", 0.0, "chat"),
        ("opacity=1&scope=app", 1.0, "app"),
        ("opacity=1.000&scope=chat", 1.0, "chat"),
        ("opacity=0.375&scope=app", 0.375, "app"),
    ],
)
def test_wallpaper_upload_grammar_accepts_canonical_metadata(
    monkeypatch, query, expected_opacity, expected_scope
) -> None:
    import api.wallpaper as wallpaper

    captured = {}

    def replace(image, *, opacity, scope):
        captured.update(image=image, opacity=opacity, scope=scope)
        return wallpaper.WallpaperInfo(True, opacity, scope, "image/png", "c" * 64)

    monkeypatch.setattr(wallpaper, "replace_wallpaper", replace)

    handler = _post_wallpaper(query)

    assert handler.status == 200
    assert captured == {
        "image": _tiny_png(),
        "opacity": expected_opacity,
        "scope": expected_scope,
    }
    assert handler.close_connection is True


def _raw_upload_request(content_length: int, body: bytes = b"") -> bytes:
    return (
        b"POST /api/wallpaper?opacity=0.8&scope=chat HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/octet-stream\r\n"
        + f"Content-Length: {content_length}\r\n".encode("ascii")
        + b"\r\n"
        + body
    )


def _serve_wallpaper_requests(monkeypatch):
    import api.routes as routes

    class RouteHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        dispatched = []

        def do_POST(self):
            self.__class__.dispatched.append(self.path)
            routes.handle_post(self, urlparse(self.path))

        def do_GET(self):
            self.__class__.dispatched.append(self.path)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RouteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, RouteHandler


def _read_all(sock: socket.socket) -> bytes:
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def test_wallpaper_upload_short_body_returns_400_and_closes(monkeypatch) -> None:
    import api.routes as routes

    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_TIMEOUT_SECONDS", 0.25, raising=False)
    server, thread, _ = _serve_wallpaper_requests(monkeypatch)
    try:
        sock = socket.create_connection(server.server_address, timeout=1)
        sock.settimeout(1)
        sock.sendall(_raw_upload_request(10, b"short"))
        sock.shutdown(socket.SHUT_WR)
        response = _read_all(sock)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert b" 400 " in response
    assert response.count(b"HTTP/1.1") == 1


def test_wallpaper_upload_deadline_bounds_drip_feed(monkeypatch) -> None:
    import api.routes as routes

    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_TIMEOUT_SECONDS", 0.25, raising=False)
    server, thread, _ = _serve_wallpaper_requests(monkeypatch)
    started = time.monotonic()
    try:
        sock = socket.create_connection(server.server_address, timeout=1)
        sock.settimeout(1)
        request = _raw_upload_request(5)
        sock.sendall(request)
        for _ in range(4):
            time.sleep(0.09)
            try:
                sock.sendall(b"x")
            except OSError:
                break
        response = _read_all(sock)
        response_elapsed = time.monotonic() - started
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response_elapsed < 0.75
    assert b" 400 " in response
    assert response.count(b"HTTP/1.1") == 1


@pytest.mark.parametrize(
    "trailing",
    [
        b"X",
        b"GET /should-not-run HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
    ],
)
def test_wallpaper_upload_trailing_or_pipeline_is_rejected_and_not_dispatched(
    monkeypatch, trailing
) -> None:
    import api.routes as routes
    import api.wallpaper as wallpaper

    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_TIMEOUT_SECONDS", 0.25, raising=False)
    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_FRAMING_GRACE_SECONDS", 0.10, raising=False)
    monkeypatch.setattr(
        wallpaper,
        "replace_wallpaper",
        lambda image, *, opacity, scope: wallpaper.WallpaperInfo(
            True, opacity, scope, "image/png", "d" * 64
        ),
    )
    image = _tiny_png()
    server, thread, handler_type = _serve_wallpaper_requests(monkeypatch)
    try:
        sock = socket.create_connection(server.server_address, timeout=1)
        sock.settimeout(1)
        sock.sendall(_raw_upload_request(len(image), image + trailing))
        response = _read_all(sock)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert b" 400 " in response
    assert response.count(b"HTTP/1.1") == 1
    assert handler_type.dispatched == ["/api/wallpaper?opacity=0.8&scope=chat"]


def test_wallpaper_upload_success_closes_connection_after_framing_grace(monkeypatch) -> None:
    import api.routes as routes
    import api.wallpaper as wallpaper

    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_TIMEOUT_SECONDS", 0.25, raising=False)
    monkeypatch.setattr(routes, "WALLPAPER_UPLOAD_FRAMING_GRACE_SECONDS", 0.10, raising=False)
    monkeypatch.setattr(
        wallpaper,
        "replace_wallpaper",
        lambda image, *, opacity, scope: wallpaper.WallpaperInfo(
            True, opacity, scope, "image/png", "e" * 64
        ),
    )
    image = _tiny_png()
    server, thread, _ = _serve_wallpaper_requests(monkeypatch)
    try:
        sock = socket.create_connection(server.server_address, timeout=1)
        sock.settimeout(1)
        sock.sendall(_raw_upload_request(len(image), image))
        response = _read_all(sock)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert b" 200 " in response
    assert response.count(b"HTTP/1.1") == 1


class _UnreadableBody:
    def read(self, *args, **kwargs):
        pytest.fail("bodyless DELETE must not read rfile")


def _wallpaper_method_handler(
    method: str,
    raw_body: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
) -> _RawUploadHandler:
    import api.routes as routes

    default_headers = []
    if method == "PATCH":
        default_headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(raw_body))),
        ]
    handler = _RawUploadHandler(raw_body, headers if headers is not None else default_headers)
    getattr(routes, f"handle_{method.lower()}")(
        handler, urlparse("http://example.com/api/wallpaper")
    )
    return handler


@pytest.mark.parametrize(
    "raw_body",
    [
        b"{}",
        b'{"opacity":0.5}',
        b'{"scope":"chat"}',
        b'{"opacity":0.5,"scope":"chat","extra":1}',
        b'{"opacity":0.5,"scope":"chat","wallpaper_file":"x"}',
        b'{"opacity":true,"scope":"chat"}',
        b'{"opacity":"0.5","scope":"chat"}',
        b'{"opacity":-0.1,"scope":"chat"}',
        b'{"opacity":1.1,"scope":"chat"}',
        b'{"opacity":NaN,"scope":"chat"}',
        b'{"opacity":Infinity,"scope":"chat"}',
        b'{"opacity":0.5,"scope":"desktop"}',
        b'{"opacity":0.5,"opacity":0.6,"scope":"chat"}',
        b'[]',
        b'{bad json}',
    ],
)
def test_wallpaper_patch_rejects_nonexact_metadata(raw_body) -> None:
    handler = _wallpaper_method_handler("PATCH", raw_body)

    assert handler.status == 400
    assert set(handler.json_body()) == {"error"}


def test_wallpaper_patch_updates_metadata_without_changing_version(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    expected = wallpaper.WallpaperInfo(True, 0.35, "app", "image/png", "f" * 64)
    calls = []
    monkeypatch.setattr(
        wallpaper,
        "update_wallpaper_metadata",
        lambda *, opacity, scope: calls.append((opacity, scope)) or expected,
    )

    handler = _wallpaper_method_handler(
        "PATCH", b'{"opacity":0.35,"scope":"app"}'
    )

    assert handler.status == 200
    assert calls == [(0.35, "app")]
    assert handler.json_body() == {
        "has_wallpaper": True,
        "opacity": 0.35,
        "scope": "app",
        "mime_type": "image/png",
        "image_version": "f" * 64,
    }


def test_wallpaper_patch_missing_active_image_returns_404(monkeypatch) -> None:
    import api.wallpaper as wallpaper

    def missing(**kwargs):
        raise wallpaper.WallpaperNotFoundError("absent")

    monkeypatch.setattr(wallpaper, "update_wallpaper_metadata", missing)

    handler = _wallpaper_method_handler(
        "PATCH", b'{"opacity":0.35,"scope":"chat"}'
    )

    assert handler.status == 404
    assert set(handler.json_body()) == {"error"}


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ([], 200),
        ([('Content-Length', '0')], 200),
        ([('Content-Length', '00')], 200),
        ([('Transfer-Encoding', 'chunked')], 400),
        ([('Content-Length', '')], 400),
        ([('Content-Length', '+0')], 400),
        ([('Content-Length', ' 0')], 400),
        ([('Content-Length', '0,0')], 400),
        ([('Content-Length', '0'), ('Content-Length', '0')], 400),
        ([('Content-Length', '1')], 400),
    ],
)
def test_wallpaper_delete_validates_bodyless_framing_and_never_reads(
    monkeypatch, headers, expected_status
) -> None:
    import api.wallpaper as wallpaper

    calls = []
    monkeypatch.setattr(
        wallpaper,
        "clear_wallpaper",
        lambda: calls.append(True) or wallpaper.WallpaperInfo(
            False, 0.8, "chat", None, None
        ),
    )
    handler = _RawUploadHandler(b"unread", headers)
    handler.rfile = _UnreadableBody()

    from api.routes import handle_delete

    handle_delete(handler, urlparse("http://example.com/api/wallpaper"))

    assert handler.status == expected_status
    if expected_status == 200:
        assert calls == [True]
        assert handler.json_body() == {
            "has_wallpaper": False,
            "opacity": 0.8,
            "scope": "chat",
            "mime_type": None,
            "image_version": None,
        }
    else:
        assert calls == []
        assert set(handler.json_body()) == {"error"}
        assert handler.close_connection is True


def test_server_marks_wallpaper_mutation_close_before_auth(monkeypatch) -> None:
    import server

    handler = server.Handler.__new__(server.Handler)
    handler.command = "POST"
    handler.path = "/api/wallpaper?opacity=0.8&scope=chat"
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 1)
    handler._headers_buffer = []
    handler.close_connection = False
    handler.send_header = lambda name, value: handler._headers_buffer.append(
        f"{name}: {value}\r\n".encode()
    )
    handler.end_headers = lambda: None
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)

    observed = {}

    def reject_auth(request, parsed):
        observed["flag"] = getattr(request, "_wallpaper_mutation_close", False)
        observed["close"] = request.close_connection
        return False

    monkeypatch.setattr(server, "check_auth", reject_auth)

    handler._handle_write(lambda *_args: pytest.fail("route must not run"))

    assert observed == {"flag": True, "close": True}


def test_server_end_headers_advertises_wallpaper_close_once(monkeypatch) -> None:
    import server

    handler = server.Handler.__new__(server.Handler)
    handler._wallpaper_mutation_close = True
    handler.request_version = "HTTP/1.1"
    handler._headers_buffer = [b"HTTP/1.1 401 Unauthorized\r\n"]
    monkeypatch.setattr(
        server.Handler,
        "csp_report_only_policy",
        classmethod(lambda cls, *args: "default-src 'none'"),
    )
    monkeypatch.setattr(
        "http.server.BaseHTTPRequestHandler.end_headers", lambda self: None
    )

    server.Handler.end_headers(handler)
    server.Handler.end_headers(handler)

    connection_headers = [
        line for line in handler._headers_buffer if line.lower().startswith(b"connection:")
    ]
    assert connection_headers == [b"Connection: close\r\n"]


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        ("validation", 400),
        ("too_large", 413),
        ("not_found", 404),
        ("collision", 500),
        ("unavailable", 500),
        ("persistence", 500),
        ("unexpected", 500),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_wallpaper_exception_mapping_is_exact_and_path_free(
    monkeypatch, method, exception, expected_status
) -> None:
    import api.wallpaper as wallpaper

    failures = {
        "validation": wallpaper.WallpaperValidationError("bad /tmp/private.png"),
        "too_large": wallpaper.WallpaperTooLargeError("huge /tmp/private.png"),
        "not_found": wallpaper.WallpaperNotFoundError("missing /tmp/private.png"),
        "collision": wallpaper.WallpaperCollisionError("collision /tmp/private.png"),
        "unavailable": wallpaper.WallpaperUnavailableError("unavailable /tmp/private.png"),
        "persistence": wallpaper.WallpaperPersistenceError(
            wallpaper.WallpaperPersistenceError.COMMITTED_OR_INDETERMINATE
        ),
        "unexpected": RuntimeError("boom /tmp/private.png errno 5"),
    }
    failure = failures[exception]

    def raise_failure(*args, **kwargs):
        raise failure

    if method == "POST":
        monkeypatch.setattr(wallpaper, "replace_wallpaper", raise_failure)
        handler = _post_wallpaper("opacity=0.8&scope=chat")
    elif method == "PATCH":
        monkeypatch.setattr(wallpaper, "update_wallpaper_metadata", raise_failure)
        handler = _wallpaper_method_handler(
            "PATCH", b'{"opacity":0.8,"scope":"chat"}'
        )
    else:
        monkeypatch.setattr(wallpaper, "clear_wallpaper", raise_failure)
        handler = _wallpaper_method_handler("DELETE")

    assert handler.status == expected_status
    assert set(handler.json_body()) == {"error"}
    assert "/tmp" not in handler.json_body()["error"]
    assert "private.png" not in handler.json_body()["error"]
    assert "errno" not in handler.json_body()["error"].lower()
