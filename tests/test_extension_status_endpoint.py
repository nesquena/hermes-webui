"""Tests for sanitized WebUI extension diagnostics."""

from types import SimpleNamespace

import pytest


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


@pytest.fixture(autouse=True)
def _clear_extension_env(monkeypatch):
    from api import auth as auth_mod

    for name in (
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "HERMES_WEBUI_EXTENSION_STYLESHEET_URLS",
        "HERMES_WEBUI_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    auth_mod._invalidate_password_hash_cache()
    yield
    auth_mod._invalidate_password_hash_cache()


def test_extension_status_disabled_by_default():
    from api.extensions import get_extension_status

    assert get_extension_status() == {
        "enabled": False,
        "extension_dir_configured": False,
        "extension_dir_valid": False,
        "script_urls": [],
        "stylesheet_urls": [],
        "sidecars": [],
        "counts": {"script_urls": 0, "stylesheet_urls": 0, "sidecars": 0},
        "manifest": {
            "configured": False,
            "loaded": False,
            "status": "not_configured",
            "entry_count": 0,
            "script_count": 0,
            "stylesheet_count": 0,
            "sidecar_count": 0,
        },
        "warnings": [],
    }


def test_extension_status_reports_invalid_extension_dir_without_path(tmp_path, monkeypatch):
    missing = tmp_path / "missing-extension-dir"
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(missing))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["enabled"] is False
    assert status["extension_dir_configured"] is True
    assert status["extension_dir_valid"] is False
    assert status["manifest"]["status"] == "extension_disabled"
    assert status["warnings"] == [
        {"code": "extension_dir_unavailable", "source": "extension_dir"}
    ]
    assert str(missing) not in repr(status)


def test_extension_status_reports_loaded_manifest_counts_and_urls(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "scripts": ["runtime.js"],
          "stylesheets": ["base.css"],
          "extensions": [
            {"id": "templates", "scripts": ["templates/app.js"], "stylesheets": ["templates/app.css"]},
            {"id": "off", "enabled": false, "scripts": ["off.js"]}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_SCRIPT_URLS", "/extensions/env.js")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["enabled"] is True
    assert status["extension_dir_configured"] is True
    assert status["extension_dir_valid"] is True
    assert status["script_urls"] == [
        "/extensions/runtime.js",
        "/extensions/templates/app.js",
        "/extensions/env.js",
    ]
    assert status["stylesheet_urls"] == [
        "/extensions/base.css",
        "/extensions/templates/app.css",
    ]
    assert status["sidecars"] == []
    assert status["counts"] == {"script_urls": 3, "stylesheet_urls": 2, "sidecars": 0}
    assert status["manifest"] == {
        "configured": True,
        "loaded": True,
        "status": "loaded",
        "entry_count": 2,
        "script_count": 2,
        "stylesheet_count": 2,
        "sidecar_count": 0,
    }
    assert status["warnings"] == []


def test_extension_status_ignores_non_dict_manifest_extensions_in_entry_count(
    tmp_path, monkeypatch
):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            null,
            "not-an-extension",
            {"id": "templates", "scripts": ["templates/app.js"]},
            {"id": "off", "enabled": false, "scripts": ["off.js"]}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/templates/app.js"]
    assert status["manifest"]["entry_count"] == 2
    assert status["manifest"]["script_count"] == 1
    assert status["manifest"]["sidecar_count"] == 0
    assert status["sidecars"] == []
    assert status["warnings"] == []


def test_extension_status_reports_missing_manifest_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "missing.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["manifest"]["status"] == "missing"
    assert status["manifest"]["loaded"] is False
    assert status["warnings"] == [{"code": "manifest_missing", "source": "manifest"}]
    assert str(root) not in repr(status)
    assert "missing.json" not in repr(status)


def test_extension_status_reports_malformed_manifest_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "bad.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["manifest"]["status"] == "malformed"
    assert status["warnings"] == [{"code": "manifest_malformed", "source": "manifest"}]
    assert "bad.json" not in repr(status)


def test_extension_status_reports_unreadable_manifest_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "bad-utf8.json").write_bytes(b"\xff\xfe")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "bad-utf8.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["manifest"]["status"] == "unreadable"
    assert status["warnings"] == [{"code": "manifest_unreadable", "source": "manifest"}]
    assert "bad-utf8.json" not in repr(status)


def test_extension_status_reports_manifest_disabled_when_dir_unconfigured(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["enabled"] is False
    assert status["extension_dir_configured"] is False
    assert status["extension_dir_valid"] is False
    assert status["manifest"]["status"] == "extension_disabled"
    assert status["manifest"]["configured"] is True
    assert status["warnings"] == []
    assert "extensions.json" not in repr(status)


def test_extension_status_reports_oversized_manifest_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "huge.json").write_text(" " * (64 * 1024 + 1), encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "huge.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["manifest"]["status"] == "oversized"
    assert status["warnings"] == [{"code": "manifest_oversized", "source": "manifest"}]
    assert "huge.json" not in repr(status)


def test_extension_status_reports_invalid_manifest_path_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "../outside.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["manifest"]["status"] == "invalid_path"
    assert status["warnings"] == [
        {"code": "manifest_invalid_path", "source": "manifest"}
    ]
    assert "outside.json" not in repr(status)
    assert str(root) not in repr(status)


def test_extension_status_reports_recursion_error_safely(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "deep.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "deep.json")

    import api.extensions as extensions

    def raise_recursion_error(_manifest_file):
        raise RecursionError("manifest nesting exceeded")

    monkeypatch.setattr(extensions, "_read_manifest_text", raise_recursion_error)

    status = extensions.get_extension_status()
    assert status["manifest"]["status"] == "too_deeply_nested"
    assert status["warnings"] == [
        {"code": "manifest_too_deeply_nested", "source": "manifest"}
    ]
    assert "deep.json" not in repr(status)


def test_extension_status_reports_rejected_assets_without_rejected_values(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "scripts": ["safe.js", "https://evil.example/app.js", "../escape.js"],
          "stylesheets": ["safe.css", "nested/../escape.css"]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/safe.js"]
    assert status["stylesheet_urls"] == ["/extensions/safe.css"]
    assert {tuple(sorted(item.items())) for item in status["warnings"]} == {
        tuple(sorted({"code": "asset_url_rejected", "source": "manifest:scripts"}.items())),
        tuple(sorted({"code": "asset_url_rejected", "source": "manifest:stylesheets"}.items())),
    }
    rendered = repr(status)
    assert "evil.example" not in rendered
    assert "escape.js" not in rendered
    assert "escape.css" not in rendered


def test_extension_status_reports_rejected_env_assets_without_rejected_values(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv(
        "HERMES_WEBUI_EXTENSION_SCRIPT_URLS",
        "/extensions/safe.js, https://evil.example/env.js",
    )

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/safe.js"]
    assert status["warnings"] == [
        {"code": "asset_url_rejected", "source": "HERMES_WEBUI_EXTENSION_SCRIPT_URLS"}
    ]
    rendered = repr(status)
    assert "evil.example" not in rendered
    assert "env.js" not in rendered


def test_extension_status_reports_sanitized_loopback_sidecars(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "desktop-companion",
              "name": "Desktop Companion",
              "scripts": ["companion-adapter.js"],
              "stylesheets": ["companion-adapter.css"],
              "sidecar": {
                "type": "loopback",
                "origin": "http://127.0.0.1:17787",
                "health_path": "/health"
              }
            },
            {
              "id": "implicit-health",
              "sidecar": {
                "type": "loopback",
                "origin": "http://localhost:17788"
              }
            },
            {
              "id": "ipv6-loopback",
              "sidecar": {
                "type": "loopback",
                "origin": "http://[::1]:17789",
                "health_path": "/ready"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/companion-adapter.js"]
    assert status["stylesheet_urls"] == ["/extensions/companion-adapter.css"]
    assert status["sidecars"] == [
        {
            "id": "desktop-companion",
            "name": "Desktop Companion",
            "type": "loopback",
            "origin": "http://127.0.0.1:17787",
            "health_path": "/health",
            "health_url": "http://127.0.0.1:17787/health",
        },
        {
            "id": "implicit-health",
            "name": "",
            "type": "loopback",
            "origin": "http://localhost:17788",
            "health_path": "/health",
            "health_url": "http://localhost:17788/health",
        },
        {
            "id": "ipv6-loopback",
            "name": "",
            "type": "loopback",
            "origin": "http://[::1]:17789",
            "health_path": "/ready",
            "health_url": "http://[::1]:17789/ready",
        },
    ]
    assert status["counts"]["sidecars"] == 3
    assert status["manifest"]["sidecar_count"] == 3
    assert status["warnings"] == []


def test_extension_status_skips_disabled_sidecar_entries(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "off",
              "enabled": false,
              "sidecar": {"type": "loopback", "origin": "http://127.0.0.1:17787"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["sidecars"] == []
    # The top-level manifest object is still inspected; the disabled extension
    # entry is skipped and must not contribute a sidecar.
    assert status["manifest"]["entry_count"] == 1
    assert status["manifest"]["sidecar_count"] == 0
    assert status["warnings"] == []


def test_extension_status_rejects_non_loopback_sidecars_without_raw_value_leak(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "bad-origin",
              "sidecar": {
                "type": "loopback",
                "origin": "http://10.0.0.5:17787",
                "health_path": "/health"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["sidecars"] == []
    assert status["manifest"]["sidecar_count"] == 0
    assert status["warnings"] == [
        {"code": "sidecar_origin_rejected", "source": "manifest:sidecars"}
    ]
    rendered = repr(status)
    assert "10.0.0.5" not in rendered
    assert "17787" not in rendered


def test_extension_status_rejects_invalid_sidecar_health_path_without_leak(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "bad-health",
              "sidecar": {
                "type": "loopback",
                "origin": "http://127.0.0.1:17787",
                "health_path": "/../secret-health"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["sidecars"] == []
    assert status["manifest"]["sidecar_count"] == 0
    assert status["warnings"] == [
        {"code": "sidecar_health_path_rejected", "source": "manifest:sidecars"}
    ]
    assert "secret-health" not in repr(status)


def test_extension_status_rejects_decoded_whitespace_sidecar_health_path(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "bad-health-space",
              "sidecar": {
                "type": "loopback",
                "origin": "http://127.0.0.1:17787",
                "health_path": "/health%20check"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["sidecars"] == []
    assert status["warnings"] == [
        {"code": "sidecar_health_path_rejected", "source": "manifest:sidecars"}
    ]
    rendered = repr(status)
    assert "health check" not in rendered
    assert "health%20check" not in rendered


def test_extension_status_rejects_encoded_query_or_fragment_sidecar_health_path(tmp_path, monkeypatch):
    """#4612 (Codex gate): the raw query/fragment ban runs BEFORE percent-decoding,
    so an encoded delimiter ("/health%3Ftoken=abc" -> "?token=abc", "/health%23frag"
    -> "#frag") must be re-rejected on the decoded path — otherwise a query/fragment
    survives into the browser-probed health URL despite the documented ban."""
    for bad_path, leaked in (
        ("/health%3Ftoken=abc", "token=abc"),
        ("/health%23frag", "frag"),
    ):
        root = tmp_path / f"extensions_{leaked}"
        root.mkdir()
        (root / "extensions.json").write_text(
            """
            {
              "extensions": [
                {
                  "id": "bad-health-delim",
                  "sidecar": {
                    "type": "loopback",
                    "origin": "http://127.0.0.1:17787",
                    "health_path": "%s"
                  }
                }
              ]
            }
            """ % bad_path,
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
        monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

        import importlib
        import api.extensions as _ext
        importlib.reload(_ext)
        status = _ext.get_extension_status()
        assert status["sidecars"] == [], f"{bad_path} must be rejected, not probed"
        assert {"code": "sidecar_health_path_rejected", "source": "manifest:sidecars"} in status["warnings"]
        rendered = repr(status)
        assert leaked not in rendered, f"rejected {bad_path} must not leak {leaked!r} into status"


def test_extension_status_rejects_unsupported_sidecar_type_without_origin_probe(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "unsupported",
              "sidecar": {"type": "unix-socket", "origin": "http://127.0.0.1:17787"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["sidecars"] == []
    assert status["warnings"] == [
        {"code": "sidecar_type_unsupported", "source": "manifest:sidecars"}
    ]


def test_extension_status_truncates_many_sidecars_with_sanitized_warning(tmp_path, monkeypatch):
    import json

    root = tmp_path / "extensions"
    root.mkdir()
    entries = [
        {
            "id": f"sidecar-{index}",
            "sidecar": {
                "type": "loopback",
                "origin": f"http://127.0.0.1:{18000 + index}",
            },
        }
        for index in range(40)
    ]
    (root / "extensions.json").write_text(
        json.dumps({"extensions": entries}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert len(status["sidecars"]) == 32
    assert status["counts"]["sidecars"] == 32
    assert status["manifest"]["sidecar_count"] == 32
    assert status["warnings"] == [
        {"code": "sidecar_list_truncated", "source": "manifest:sidecars"}
    ]



def test_extension_status_route_is_wired(monkeypatch):
    from api import routes

    captured = {}

    def fake_j(handler, data, status=200, headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    handler = FakeHandler()
    assert routes.handle_get(handler, SimpleNamespace(path="/api/extensions/status")) is True
    assert captured["status"] == 200
    assert captured["data"]["enabled"] is False


def test_extension_status_route_requires_webui_auth(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api.auth import check_auth

    handler = FakeHandler()
    assert check_auth(handler, SimpleNamespace(path="/api/extensions/status", query="")) is False
    assert handler.status == 401
    assert handler.header("Location") is None
