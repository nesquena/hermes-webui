"""Opus pre-release follow-up tests for stage-265 (PR #1445 extension hooks).

These tests pin the defense-in-depth additions from the Opus advisor review:
- `_fully_unquote_path` iterates up to 10 times (catches quadruple-encoded ..)
- `_read_url_list` caps at `_MAX_URL_LIST` (32) entries
- `_read_url_list` logs once per rejected URL
- MIME map covers `ttf`, `otf`, `wasm` for modern font/wasm assets
"""

import logging
from types import SimpleNamespace


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

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


def test_fully_unquote_handles_quadruple_encoded(monkeypatch):
    """Quadruple-encoded `..` (`%2525252e%2525252e`) must collapse to literal
    `..` so the segment-level safety check rejects it. The original 3-iteration
    cap stopped at `%2e%2e` and would have accepted the URL into the validator.
    """
    from api.extensions import _fully_unquote_path

    # Plain percent-encoding stops at `..` after 1 unquote
    assert (
        _fully_unquote_path("/extensions/%2e%2e/api/session")
        == "/extensions/../api/session"
    )
    # Double-encoded after 2 unquotes
    assert (
        _fully_unquote_path("/extensions/%252e%252e/api/session")
        == "/extensions/../api/session"
    )
    # Triple-encoded after 3 unquotes
    assert (
        _fully_unquote_path("/extensions/%25252e%25252e/api/session")
        == "/extensions/../api/session"
    )
    # Quadruple-encoded after 4 unquotes — the case that slipped through the
    # original `range(3)` and reached the validator unchanged
    assert (
        _fully_unquote_path("/extensions/%2525252e%2525252e/api/session")
        == "/extensions/../api/session"
    )


def test_quadruple_encoded_traversal_url_now_rejected(tmp_path, monkeypatch):
    """End-to-end: quadruple-encoded `..` in a configured URL is rejected
    by the validator instead of slipping through. Pre-Opus this passed.
    """
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "/extensions/%2525252e%2525252e/api/session, /extensions/legit.js",
    )

    from api.extensions import get_extension_config

    config = get_extension_config()
    # Only the legit URL should pass validation; the quadruple-encoded
    # traversal must be filtered out
    assert config["script_urls"] == ["/extensions/legit.js"]


def test_url_list_caps_at_max(tmp_path, monkeypatch):
    """Configured URL lists cap at _MAX_URL_LIST entries to avoid pathological
    rendering when a misconfigured env var ships thousands of URLs.
    """
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))

    # Build 100 valid URLs
    urls = ", ".join(f"/extensions/script{i}.js" for i in range(100))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", urls)

    from api.extensions import get_extension_config, _MAX_URL_LIST

    config = get_extension_config()
    assert len(config["script_urls"]) == _MAX_URL_LIST
    # First N kept (insertion order)
    assert config["script_urls"][0] == "/extensions/script0.js"
    assert config["script_urls"][-1] == f"/extensions/script{_MAX_URL_LIST - 1}.js"


def test_url_list_logs_rejected_urls_once(tmp_path, monkeypatch, caplog):
    """A misconfigured URL must produce a one-shot warning so an admin who
    typos `https://...` (rejected as external) sees a signal in logs instead
    of just a silently-not-loading extension.
    """
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "https://evil.example.com/x.js, /extensions/legit.js",
    )

    # Reset the per-process warning cache so the test doesn't accidentally
    # depend on state from other tests in the same run
    from api.extensions import _warned_urls

    _warned_urls.clear()

    caplog.set_level(logging.WARNING, logger="api.extensions")

    from api.extensions import get_extension_config

    config = get_extension_config()
    assert config["script_urls"] == ["/extensions/legit.js"]

    # The external URL must surface as a warning in the log
    assert any(
        "Rejected extension URL" in record.message
        and "evil.example.com" in record.message
        for record in caplog.records
    )

    # Second call within the same process must not re-log (one-shot)
    caplog.clear()
    config2 = get_extension_config()
    assert config2["script_urls"] == ["/extensions/legit.js"]
    rejection_records = [
        r
        for r in caplog.records
        if "Rejected extension URL" in r.message and "evil.example.com" in r.message
    ]
    assert rejection_records == [], (
        "Repeated invalid URL should not re-log on every config read"
    )


def test_expanded_mime_map_serves_fonts_and_wasm(tmp_path, monkeypatch):
    """`ttf`, `otf`, and `wasm` extensions must serve with the right
    Content-Type so browsers don't reject (especially `.wasm`, which Chrome
    refuses to instantiate when served as `text/plain`).
    """
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "font.ttf").write_bytes(b"fake ttf binary")
    (root / "font.otf").write_bytes(b"fake otf binary")
    (root / "module.wasm").write_bytes(b"\x00asm" + b"\x01" * 8)

    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))

    from api.extensions import serve_extension_static

    ttf = FakeHandler()
    assert (
        serve_extension_static(ttf, SimpleNamespace(path="/extensions/font.ttf"))
        is True
    )
    assert ttf.status == 200
    assert ttf.header("Content-Type") == "font/ttf"

    otf = FakeHandler()
    assert (
        serve_extension_static(otf, SimpleNamespace(path="/extensions/font.otf"))
        is True
    )
    assert otf.status == 200
    assert otf.header("Content-Type") == "font/otf"

    wasm = FakeHandler()
    assert (
        serve_extension_static(wasm, SimpleNamespace(path="/extensions/module.wasm"))
        is True
    )
    assert wasm.status == 200
    assert wasm.header("Content-Type") == "application/wasm"
    # Binary types must NOT have a charset suffix
    assert "charset" not in wasm.header("Content-Type")
