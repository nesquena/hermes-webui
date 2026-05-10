"""Tests for opt-in WebUI extension hooks.

The extension surface must stay deliberately small and safe:
- disabled unless configured by environment
- same-origin script/style URLs only
- no filesystem path leakage in public config
- static assets sandboxed to the configured extension directory
"""

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


def test_extension_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": False,
        "script_urls": [],
        "stylesheet_urls": [],
    }


def test_extension_config_accepts_only_safe_same_origin_urls(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        ", ".join(
            [
                "/extensions/app.js",
                "https://example.com/evil.js",
                "//example.com/evil.js",
                "javascript:alert(1)",
                "/api/session",
                "/extensions/../api/session",
                "/extensions/%2e%2e/api/session",
                "/extensions/%252e%252e/api/session",
                "/static/../api/session",
            ]
        ),
    )
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS",
        "/extensions/app.css, /static/theme.css, data:text/css,body{}",
    )

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/app.js"],
        "stylesheet_urls": ["/extensions/app.css", "/static/theme.css"],
    }


def test_index_html_injection_escapes_urls_and_preserves_disabled_default(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_DIR", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/app.js")

    from api.extensions import inject_extension_tags

    html = "<html><head></head><body><main></main></body></html>"
    assert inject_extension_tags(html) == html

    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/app.js?v=1&mode=dev"
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", "/extensions/app.css")

    injected = inject_extension_tags(html)

    assert '<link rel="stylesheet" href="/extensions/app.css">' in injected
    assert (
        '<script src="/extensions/app.js?v=1&amp;mode=dev" defer></script>' in injected
    )
    assert injected.index("/extensions/app.css") < injected.index("</head>")
    assert injected.index("/extensions/app.js") < injected.index("</body>")


def test_extension_route_remains_behind_webui_auth(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api.auth import check_auth

    extension = FakeHandler()
    # SimpleNamespace must include `query` because api.auth.check_auth (since
    # v0.50.258, the multi-param ?next= encoding fix) accesses `parsed.query`
    # when constructing the redirect Location header.
    assert (
        check_auth(extension, SimpleNamespace(path="/extensions/app.js", query=""))
        is False
    )
    assert extension.status == 302
    assert extension.header("Location") == "login?next=/extensions/app.js"

    # Existing core static assets remain public; extension assets intentionally
    # do not share that exemption because they are administrator-supplied code.
    static = FakeHandler()
    assert check_auth(static, SimpleNamespace(path="/static/ui.js", query="")) is True


def test_extension_static_serving_is_sandboxed(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "app.js").write_text("window.extensionLoaded = true;", encoding="utf-8")
    (root / ".secret").write_text("do not serve", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))

    from api.extensions import serve_extension_static

    ok = FakeHandler()
    assert (
        serve_extension_static(ok, SimpleNamespace(path="/extensions/app.js")) is True
    )
    assert ok.status == 200
    assert ok.header("Content-Type") == "application/javascript; charset=utf-8"
    assert bytes(ok.body) == b"window.extensionLoaded = true;"

    traversal = FakeHandler()
    assert (
        serve_extension_static(
            traversal, SimpleNamespace(path="/extensions/../outside.txt")
        )
        is True
    )
    assert traversal.status == 404

    encoded_traversal = FakeHandler()
    assert (
        serve_extension_static(
            encoded_traversal, SimpleNamespace(path="/extensions/%2e%2e/outside.txt")
        )
        is True
    )
    assert encoded_traversal.status == 404

    dotfile = FakeHandler()
    assert (
        serve_extension_static(dotfile, SimpleNamespace(path="/extensions/.secret"))
        is True
    )
    assert dotfile.status == 404


def test_extension_static_serving_fails_closed_when_disabled_or_unreadable(
    tmp_path, monkeypatch
):
    missing_root = tmp_path / "missing"
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(missing_root))

    from api.extensions import serve_extension_static

    disabled = FakeHandler()
    assert (
        serve_extension_static(disabled, SimpleNamespace(path="/extensions/app.js"))
        is True
    )
    assert disabled.status == 404

    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    (root / "nested").mkdir()
    (root / "nested" / "app.js").write_text("ok", encoding="utf-8")

    encoded_slash_traversal = FakeHandler()
    assert (
        serve_extension_static(
            encoded_slash_traversal,
            SimpleNamespace(path="/extensions/nested%2f..%2f..%2foutside.txt"),
        )
        is True
    )
    assert encoded_slash_traversal.status == 404

    encoded_backslash = FakeHandler()
    assert (
        serve_extension_static(
            encoded_backslash, SimpleNamespace(path="/extensions/nested%5capp.js")
        )
        is True
    )
    assert encoded_backslash.status == 404


def test_extension_static_serving_rejects_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    symlink = root / "outside-link.txt"

    try:
        symlink.symlink_to(outside)
    except OSError:
        # Some platforms/filesystems disallow symlink creation. The path-safety
        # behavior is still covered by traversal tests above.
        return

    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))

    from api.extensions import serve_extension_static

    escaped = FakeHandler()
    assert (
        serve_extension_static(
            escaped, SimpleNamespace(path="/extensions/outside-link.txt")
        )
        is True
    )
    assert escaped.status == 404
