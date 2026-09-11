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
from urllib.parse import quote, urlparse

import api.config as api_config
import api.routes as routes
from api.updates import WEBUI_VERSION
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
    os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert len(second_bytes) == len(first_bytes)
    restored_stat = bundle.stat()
    assert restored_stat.st_size == stat.st_size
    assert restored_stat.st_mtime_ns == stat.st_mtime_ns
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


def test_unavailable_identity_does_not_authorize_cached_shell_reuse(
    tmp_path, monkeypatch
):
    """An unreadable inventoried asset must not make version-only metadata stable."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    index_path = static_root / "index.html"
    index_path.write_text(
        'old <script src="static/ui.js?v=__WEBUI_VERSION__"></script>',
        encoding="utf-8",
    )
    bundle = static_root / "ui.js"
    bundle.write_bytes(b"alpha")
    (static_root / "sw.js").write_text(
        "const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';",
        encoding="utf-8",
    )
    unavailable = static_root / "unreadable.txt"
    unavailable.write_bytes(b"identity input")

    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: index_path)
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    real_read_bytes = Path.read_bytes

    def fail_unavailable_read(path):
        if path == unavailable:
            raise PermissionError("controlled inventory failure")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_unavailable_read)

    first = routes._render_index_shell_base()
    assert first.startswith("old ")
    assert "base" not in routes._INDEX_SHELL_CACHE

    index_stat = index_path.stat()
    index_path.write_text(
        'new <script src="static/ui.js?v=__WEBUI_VERSION__"></script>',
        encoding="utf-8",
    )
    os.utime(index_path, ns=(index_stat.st_atime_ns, index_stat.st_mtime_ns))
    bundle.write_bytes(b"bravo")
    assert index_path.stat().st_mtime_ns == index_stat.st_mtime_ns
    second = routes._render_index_shell_base()

    assert second.startswith("new ")
    assert second != first
    assert routes._ASSET_IDENTITY_UNAVAILABLE_TOKEN in second
    assert quote(WEBUI_VERSION, safe="") not in second
    assert "base" not in routes._INDEX_SHELL_CACHE

    worker_handler = _RouteHandler()
    assert routes.handle_get(worker_handler, urlparse("http://test/sw.js")) is True
    assert worker_handler.status == 503
    assert ("Cache-Control", "no-store") in worker_handler.sent_headers
    assert quote(WEBUI_VERSION, safe="") not in bytes(worker_handler.body).decode(
        "utf-8"
    )


def test_search_only_vendor_directory_fails_closed(tmp_path, monkeypatch):
    """A still-servable leaf beneath an unlistable vendor directory fails closed."""
    if os.name != "posix":
        pytest.skip("real 0111 directory semantics require POSIX")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("real 0111 directory semantics do not deny listing to root")

    static_root = tmp_path / "static"
    static_root.mkdir()
    index_path = static_root / "index.html"
    index_path.write_text(
        '<html><script src="static/ui.js?v=__WEBUI_VERSION__"></script></html>',
        encoding="utf-8",
    )
    (static_root / "ui.js").write_bytes(b"shell")
    (static_root / "sw.js").write_text(
        "const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';",
        encoding="utf-8",
    )
    vendor = static_root / "vendor" / "private"
    vendor.mkdir(parents=True)
    leaf = vendor / "known-leaf.js"
    leaf.write_bytes(b"export const value = 'alpha';\n")

    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: index_path)
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    # Prime the cache while the complete tree is readable, exactly as a process
    # would do before a deployment directory is made search-only.
    first_token = routes._assets_cache_bust_token(static_root)
    assert routes._asset_identity_is_available(first_token)
    first_shell = routes._render_index_shell_base()
    assert "base" in routes._INDEX_SHELL_CACHE

    vendor.chmod(0o111)
    try:
        try:
            os.listdir(vendor)
        except PermissionError:
            pass
        else:
            pytest.skip("0111 did not deny directory listing to this runner")

        leaf_stat = leaf.stat()
        leaf.write_bytes(b"export const value = 'bravo';\n")
        os.utime(leaf, ns=(leaf_stat.st_atime_ns, leaf_stat.st_mtime_ns))
        assert leaf.stat().st_size == leaf_stat.st_size
        assert leaf.stat().st_mtime_ns == leaf_stat.st_mtime_ns

        static_handler = _RouteHandler()
        assert routes._serve_static(
            static_handler,
            urlparse("http://test/static/vendor/private/known-leaf.js"),
        )
        assert static_handler.status == 200
        assert bytes(static_handler.body) == b"export const value = 'bravo';\n"

        second_token = routes._assets_cache_bust_token(static_root)
        assert not routes._asset_identity_is_available(second_token)
        assert second_token.endswith(routes._ASSET_IDENTITY_UNAVAILABLE_SUFFIX)

        second_shell = routes._render_index_shell_base()
        assert routes._ASSET_IDENTITY_UNAVAILABLE_TOKEN in second_shell
        assert second_shell is not first_shell
        assert "base" not in routes._INDEX_SHELL_CACHE

        worker_handler = _RouteHandler()
        assert routes.handle_get(worker_handler, urlparse("http://test/sw.js")) is True
        assert worker_handler.status == 503
        assert ("Cache-Control", "no-store") in worker_handler.sent_headers
        assert quote(WEBUI_VERSION, safe="") not in bytes(
            worker_handler.body
        ).decode("utf-8")
    finally:
        vendor.chmod(0o755)
