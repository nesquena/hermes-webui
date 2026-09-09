"""Regression coverage for the static-shell cache-bust token.

The token is derived from normalized resource paths and actual content bytes.
It must change when any recursive shell resource changes, even when
WEBUI_VERSION, file size, or mtime stays constant (non-git installs). Without
it, the service-worker cache name never changes and stale bundles survive
indefinitely; hard refresh does not bypass service-worker caches.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse

import api.config as api_config
import api.routes as routes
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _one_pixel_png(pixel: bytes) -> bytes:
    """Return a valid PNG so icon fixtures exercise real binary resources."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


@pytest.fixture()
def asset_token():
    """Return the real _assets_cache_bust_token bound to this worktree."""
    from api import routes

    return routes._assets_cache_bust_token


def test_token_does_not_raise_on_missing_static_root(tmp_path, asset_token):
    """Fail-soft: a missing or empty static root returns a string, not an error."""
    token = asset_token(tmp_path / "does-not-exist")
    assert isinstance(token, str)
    assert token != ""


def test_token_is_stable_for_metadata_only_touch(tmp_path, asset_token):
    """A touch without a byte edit is not a new content revision."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    bundle = static_root / "ui.js"
    bundle.write_text("console.log(1)", encoding="utf-8")
    first = asset_token(static_root)
    time.sleep(0.02)
    bundle.touch()
    second = asset_token(static_root)
    assert second == first, "metadata alone must not create a new revision"


@pytest.mark.parametrize(
    ("relative_path", "first_bytes", "second_bytes"),
    [
        ("vendor/example/example.min.js", b"console.log('a');", b"console.log('b');"),
        ("vendor/example/example.css", b".old{color:red}", b".new{color:tan}"),
        ("manifest.json", b'{"name":"aaaaaa"}', b'{"name":"bbbbbb"}'),
        ("favicon.svg", b"<svg>a</svg>", b"<svg>b</svg>"),
        (
            "favicon-32.png",
            _one_pixel_png(b"\x01\x02\x03\xff"),
            _one_pixel_png(b"\x04\x05\x06\xff"),
        ),
    ],
)
def test_token_changes_for_shell_bytes_with_stable_metadata(
    tmp_path, asset_token, relative_path, first_bytes, second_bytes
):
    """Byte identity—not metadata—must govern the shell revision."""
    static_root = tmp_path / "static"
    bundle = static_root / relative_path
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(first_bytes)
    stat = bundle.stat()
    first = asset_token(static_root)

    bundle.write_bytes(second_bytes)
    os.utime(bundle, (stat.st_atime, stat.st_mtime))

    assert len(second_bytes) == len(first_bytes)
    assert asset_token(static_root) != first


def test_token_carries_fingerprint_suffix(tmp_path, asset_token):
    """The token must differ from the bare constant version value."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "ui.js").write_text("x", encoding="utf-8")
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    token = asset_token(static_root)
    assert "%2Ba" in token, "token must carry the asset fingerprint suffix (URL-encoded)"
    assert token != "unknown", "token must not be the bare constant version"


class _RouteHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.headers = {}
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)


def test_shell_cache_and_worker_agree_after_nested_bundle_mutation(
    tmp_path, monkeypatch
):
    """A bundle edit invalidates the shell even when index.html is unchanged."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    index_path = static_root / "index.html"
    index_path.write_text(
        '<html><script src="static/ui.js?v=__WEBUI_VERSION__"></script></html>',
        encoding="utf-8",
    )
    (static_root / "sw.js").write_text(
        "const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';",
        encoding="utf-8",
    )
    bundle = static_root / "vendor" / "nested.js"
    bundle.parent.mkdir()
    bundle.write_bytes(b"export const value = 'alpha';\n")
    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: index_path)
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    def shell_token() -> str:
        shell_handler = _RouteHandler()
        routes.handle_get(shell_handler, urlparse("http://test/"))
        assert shell_handler.status == 200
        html = bytes(shell_handler.body).decode("utf-8")
        start = html.index("static/ui.js?v=") + len("static/ui.js?v=")
        return html[start:html.index('"', start)]

    first_shell_token = shell_token()
    assert "base" in routes._INDEX_SHELL_CACHE

    stat = bundle.stat()
    bundle.write_bytes(b"export const value = 'bravo';\n")
    os.utime(bundle, (stat.st_atime, stat.st_mtime))

    second_shell_token = shell_token()
    handler = _RouteHandler()
    assert routes.handle_get(handler, urlparse("http://test/sw.js")) is True
    worker_text = bytes(handler.body).decode("utf-8")
    second_worker_token = worker_text.rsplit("hermes-shell-", 1)[1].rstrip("';\n")
    assert second_shell_token != first_shell_token
    assert second_worker_token == second_shell_token


def test_sw_route_uses_bundle_fingerprint_token():
    """Source anchor: the /sw.js route must derive its cache name from the token."""
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    idx = routes_src.find('"/sw.js"')
    assert idx != -1
    block = routes_src[idx:idx + 1000]
    assert "_assets_cache_bust_token(static_root)" in block, (
        "sw.js route must derive its cache name from the bundle fingerprint"
    )
