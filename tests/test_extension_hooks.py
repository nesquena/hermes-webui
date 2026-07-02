"""Tests for opt-in WebUI extension hooks.

The extension surface must stay deliberately small and safe:
- disabled unless configured by environment
- same-origin script/style URLs only
- no filesystem path leakage in public config
- static assets sandboxed to the configured extension directory
"""

import logging
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clear_extension_env(monkeypatch):
    for name in (
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS",
    ):
        monkeypatch.delenv(name, raising=False)

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


def test_extension_config_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    # Point the managed state dir at an empty temp dir so the default extension
    # root does not exist yet — config stays disabled until the first install.
    import api.extensions as extensions

    monkeypatch.setattr(extensions, "_extension_state_dir", lambda: tmp_path)

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


def test_index_html_injection_escapes_urls_and_preserves_disabled_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_DIR", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/app.js")

    from api.extensions import inject_extension_tags

    html = "<html><head></head><body><main></main></body></html>"
    assert inject_extension_tags(html) == html

    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/app.js?v=1&mode=dev")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", "/extensions/app.css")

    injected = inject_extension_tags(html)

    assert '<link rel="stylesheet" href="/extensions/app.css">' in injected
    assert '<script src="/extensions/app.js?v=1&amp;mode=dev" defer></script>' in injected
    assert injected.index("/extensions/app.css") < injected.index("</head>")
    assert injected.index("/extensions/app.js") < injected.index("</body>")


def test_extension_settings_runtime_config_injects_before_extension_scripts(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "manifest.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "settings-ok",
              "scripts": ["settings-ok.js"],
              "permissions": {"storage": {"owned": true}},
              "settings_schema": [
                {"key": "flag", "type": "boolean", "default": true}
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api.extensions import inject_extension_tags

    injected = inject_extension_tags("<html><head></head><body></body></html>")

    assert "window.__HERMES_EXTENSION_CONFIG__" in injected
    assert "window.HermesExtensionSettings.primeFromStatus(window.__HERMES_EXTENSION_CONFIG__)" in injected
    assert '"storage_owned":true' in injected
    assert '"settings_schema":[{"key":"flag","type":"boolean"' in injected
    assert injected.index("window.__HERMES_EXTENSION_CONFIG__") < injected.index("/extensions/settings-ok.js")


def test_extension_settings_only_manifest_still_injects_runtime_config(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "manifest.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "settings-only",
              "permissions": {"storage": {"owned": true}},
              "settings_schema": [
                {"key": "flag", "type": "boolean", "default": true}
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api.extensions import inject_extension_tags

    injected = inject_extension_tags("<html><head></head><body></body></html>")

    assert "window.__HERMES_EXTENSION_CONFIG__" in injected
    assert '"id":"settings-only"' in injected
    assert injected.index("window.__HERMES_EXTENSION_CONFIG__") < injected.index("</body>")


def test_extension_route_remains_behind_webui_auth(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api.auth import check_auth, _invalidate_password_hash_cache

    # The password hash is cached process-wide (PBKDF2 is ~1s/call). Invalidate
    # before so this test reads the just-set env var rather than a stale None
    # cached by an earlier auth-disabled test, and again in finally so the hash
    # computed here can't leak into a later test that expects auth disabled.
    # Without this the test's result depends on suite execution order.
    _invalidate_password_hash_cache()
    try:
        extension = FakeHandler()
        # SimpleNamespace must include `query` because api.auth.check_auth (since
        # v0.50.258, the multi-param ?next= encoding fix) accesses `parsed.query`
        # when constructing the redirect Location header.
        assert check_auth(extension, SimpleNamespace(path="/extensions/app.js", query="")) is False
        assert extension.status == 302
        assert extension.header("Location") == "login?next=/extensions/app.js"

        # Existing core static assets remain public; extension assets intentionally
        # do not share that exemption because they are administrator-supplied code.
        static = FakeHandler()
        assert check_auth(static, SimpleNamespace(path="/static/ui.js", query="")) is True
    finally:
        _invalidate_password_hash_cache()




def test_extension_manifest_adds_bundled_assets_before_env_urls(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "manifest.json").write_text(
        """
        {
          "scripts": ["runtime.js"],
          "stylesheets": ["base.css"],
          "extensions": [
            {
              "id": "templates",
              "scripts": ["templates/app.js", "/extensions/shared.js"],
              "stylesheets": ["templates/app.css"]
            },
            {
              "id": "disabled",
              "enabled": false,
              "scripts": ["disabled.js"],
              "stylesheets": ["disabled.css"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "/extensions/templates/app.js, /extensions/env-only.js",
    )
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS",
        "/extensions/env-only.css",
    )

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": [
            "/extensions/runtime.js",
            "/extensions/templates/app.js",
            "/extensions/shared.js",
            "/extensions/env-only.js",
        ],
        "stylesheet_urls": [
            "/extensions/base.css",
            "/extensions/templates/app.css",
            "/extensions/env-only.css",
        ],
        "extensions": [
            {
                "id": "templates",
                "name": "templates",
                "storage_owned": False,
                "settings_schema": [],
            },
        ],
    }


def test_extension_manifest_relative_assets_resolve_from_manifest_directory(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    ext_dir = root / "desktop-companion"
    ext_dir.mkdir()
    (ext_dir / "manifest.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "desktop-companion",
              "scripts": ["assets/companion-adapter.js"],
              "stylesheets": ["assets/companion-adapter.css"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "desktop-companion/manifest.json")
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/desktop-companion/assets/companion-adapter.js"],
        "stylesheet_urls": ["/extensions/desktop-companion/assets/companion-adapter.css"],
        "extensions": [
            {
                "id": "desktop-companion",
                "name": "desktop-companion",
                "storage_owned": False,
                "settings_schema": [],
            }
        ],
    }


def test_extension_manifest_reuses_url_safety_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "bundle.json").write_text(
        """
        {
          "scripts": [
            "safe.js",
            "../escape.js",
            "/api/session",
            "https://example.com/evil.js",
            "/extensions/%252e%252e/api/session"
          ],
          "stylesheets": ["safe.css", "nested/../escape.css"]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "bundle.json")

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/safe.js"],
        "stylesheet_urls": ["/extensions/safe.css"],
    }


def test_extension_manifest_deeply_nested_json_fails_safe(tmp_path, monkeypatch):
    """A <=64KB but deeply-nested manifest makes json.loads raise RecursionError.
    It must fail safe (empty lists) — NOT escape and 503 every page load."""
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    root = tmp_path / "extensions"
    root.mkdir()
    # ~6000 nested arrays: well under 64KB but blows the recursion limit in json.loads.
    depth = 6000
    payload = "[" * depth + "]" * depth
    assert len(payload.encode("utf-8")) < 64 * 1024
    (root / "deep.json").write_text(payload, encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "deep.json")

    from api.extensions import get_extension_config

    # Must not raise; must fall back to no manifest assets.
    cfg = get_extension_config()
    assert cfg["script_urls"] == []
    assert cfg["stylesheet_urls"] == []


def test_extension_manifest_path_must_stay_inside_extension_root(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    root = tmp_path / "extensions"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"scripts":["outside.js"]}', encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "../outside.json")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/env.js")

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/env.js"],
        "stylesheet_urls": [],
    }


def test_extension_manifest_url_list_shares_cap_with_env_urls(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)
    root = tmp_path / "extensions"
    root.mkdir()
    scripts = ",".join(f'"script{i}.js"' for i in range(40))
    (root / "manifest.json").write_text(f'{{"scripts":[{scripts}]}}', encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/env.js")

    from api.extensions import _MAX_URL_LIST, get_extension_config

    config = get_extension_config()
    assert len(config["script_urls"]) == _MAX_URL_LIST
    assert config["script_urls"][0] == "/extensions/script0.js"
    assert config["script_urls"][-1] == f"/extensions/script{_MAX_URL_LIST - 1}.js"
    assert "/extensions/env.js" not in config["script_urls"]


def test_extension_manifest_logs_invalid_json_separately_from_oversize(tmp_path, monkeypatch, caplog):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")
    (root / "manifest.json").write_text('{"scripts": [', encoding="utf-8")

    from api.extensions import get_extension_config

    caplog.set_level(logging.WARNING, logger="api.extensions")

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": [],
        "stylesheet_urls": [],
    }
    assert any("not valid JSON" in record.message for record in caplog.records)
    assert not any("could not be read" in record.message for record in caplog.records)
    assert not any("exceeds" in record.message for record in caplog.records)


def test_extension_manifest_logs_invalid_utf8_as_unreadable(tmp_path, monkeypatch, caplog):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")
    (root / "manifest.json").write_bytes(b"{\xff}")

    from api.extensions import get_extension_config

    caplog.set_level(logging.WARNING, logger="api.extensions")

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": [],
        "stylesheet_urls": [],
    }
    assert any("could not be read" in record.message for record in caplog.records)
    assert not any("exceeds" in record.message for record in caplog.records)


def test_extension_manifest_logs_oversize_distinctly(tmp_path, monkeypatch, caplog):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api import extensions

    (root / "manifest.json").write_text(
        '{"scripts":["pwn.js"]}' + (" " * extensions._MAX_MANIFEST_BYTES),
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="api.extensions")

    assert extensions.get_extension_config() == {
        "enabled": True,
        "script_urls": [],
        "stylesheet_urls": [],
    }
    assert any("exceeds" in record.message for record in caplog.records)
    assert not any("not valid JSON" in record.message for record in caplog.records)


def test_extension_manifest_reads_only_bounded_size(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api import extensions

    # If the bounded read/cap check is removed, this parses as valid JSON and
    # would inject /extensions/pwn.js. The trailing padding makes it oversize.
    oversize = '{"scripts":["pwn.js"]}' + (" " * extensions._MAX_MANIFEST_BYTES)
    (root / "manifest.json").write_text(oversize, encoding="utf-8")

    assert extensions.get_extension_config() == {
        "enabled": True,
        "script_urls": [],
        "stylesheet_urls": [],
    }



def test_extension_manifest_multibyte_payload_is_bounded_by_bytes(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api import extensions

    oversize_multibyte = '{"scripts":["pwn.js"]}' + ("€" * extensions._MAX_MANIFEST_BYTES)
    (root / "manifest.json").write_text(oversize_multibyte, encoding="utf-8")

    assert extensions.get_extension_config() == {
        "enabled": True,
        "script_urls": [],
        "stylesheet_urls": [],
    }


def test_extension_manifest_cap_warning_logs_once_across_many_entries(tmp_path, monkeypatch, caplog):
    root = tmp_path / "extensions"
    root.mkdir()
    entries = [
        {
            "id": f"ext{i}",
            "scripts": [f"script{i}.js"],
            "stylesheets": [f"style{i}.css"],
        }
        for i in range(40)
    ]
    (root / "manifest.json").write_text(
        __import__("json").dumps({"extensions": entries}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api.extensions import _MAX_URL_LIST, _warned_urls, get_extension_config

    _warned_urls.clear()
    caplog.set_level(logging.WARNING, logger="api.extensions")

    config = get_extension_config()

    assert len(config["script_urls"]) == _MAX_URL_LIST
    assert len(config["stylesheet_urls"]) == _MAX_URL_LIST
    truncation_records = [
        record for record in caplog.records
        if "truncated at" in record.message and "Extension URL list" in record.message
    ]
    assert len(truncation_records) == 2
    assert any("manifest:scripts" in record.message for record in truncation_records)
    assert any("manifest:stylesheets" in record.message for record in truncation_records)

def test_extension_manifest_ignores_non_list_asset_fields(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "manifest.json").write_text(
        """
        {
          "scripts": "app.js",
          "stylesheets": {"href": "app.css"},
          "extensions": {
            "bad": {"scripts": ["bad.js"]}
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/env.js")
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/env.js"],
        "stylesheet_urls": [],
    }



def test_extension_env_only_duplicate_urls_preserve_legacy_behavior(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_MANIFEST", raising=False)
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "/extensions/app.js, /extensions/app.js",
    )
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/app.js", "/extensions/app.js"],
        "stylesheet_urls": [],
    }


def test_extension_manifest_accepts_top_level_extension_array(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        [
          {"id": "a", "scripts": ["a/a.js"], "stylesheets": ["a/a.css"]},
          {"id": "b", "scripts": ["b/b.js"]}
        ]
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_EXTENSION_STYLESHEET_URLS", raising=False)

    from api.extensions import get_extension_config

    assert get_extension_config() == {
        "enabled": True,
        "script_urls": ["/extensions/a/a.js", "/extensions/b/b.js"],
        "stylesheet_urls": ["/extensions/a/a.css"],
        "extensions": [
            {"id": "a", "name": "a", "storage_owned": False, "settings_schema": []},
            {"id": "b", "name": "b", "storage_owned": False, "settings_schema": []},
        ],
    }

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
    assert serve_extension_static(ok, SimpleNamespace(path="/extensions/app.js")) is True
    assert ok.status == 200
    assert ok.header("Content-Type") == "application/javascript; charset=utf-8"
    assert bytes(ok.body) == b"window.extensionLoaded = true;"

    traversal = FakeHandler()
    assert serve_extension_static(traversal, SimpleNamespace(path="/extensions/../outside.txt")) is True
    assert traversal.status == 404

    encoded_traversal = FakeHandler()
    assert serve_extension_static(encoded_traversal, SimpleNamespace(path="/extensions/%2e%2e/outside.txt")) is True
    assert encoded_traversal.status == 404

    dotfile = FakeHandler()
    assert serve_extension_static(dotfile, SimpleNamespace(path="/extensions/.secret")) is True
    assert dotfile.status == 404


def test_extension_static_serving_fails_closed_when_disabled_or_unreadable(tmp_path, monkeypatch):
    missing_root = tmp_path / "missing"
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(missing_root))

    from api.extensions import serve_extension_static

    disabled = FakeHandler()
    assert serve_extension_static(disabled, SimpleNamespace(path="/extensions/app.js")) is True
    assert disabled.status == 404

    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    (root / "nested").mkdir()
    (root / "nested" / "app.js").write_text("ok", encoding="utf-8")

    encoded_slash_traversal = FakeHandler()
    assert serve_extension_static(
        encoded_slash_traversal,
        SimpleNamespace(path="/extensions/nested%2f..%2f..%2foutside.txt"),
    ) is True
    assert encoded_slash_traversal.status == 404

    encoded_backslash = FakeHandler()
    assert serve_extension_static(encoded_backslash, SimpleNamespace(path="/extensions/nested%5capp.js")) is True
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
    assert serve_extension_static(escaped, SimpleNamespace(path="/extensions/outside-link.txt")) is True
    assert escaped.status == 404
