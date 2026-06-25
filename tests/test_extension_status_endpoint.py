"""Tests for sanitized WebUI extension diagnostics."""

from types import SimpleNamespace
import json

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


def _use_extension_state_dir(monkeypatch, tmp_path):
    state_dir = tmp_path / "webui-state"
    state_dir.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    import api.extensions as extensions

    monkeypatch.setattr(extensions, "_extension_state_dir", lambda: state_dir)
    return state_dir


def _status_counts_empty():
    return {
        "script_urls": 0,
        "stylesheet_urls": 0,
        "sidecars": 0,
        "manifest_extensions": 0,
        "user_disabled": 0,
    }


def test_extension_status_disabled_by_default(tmp_path, monkeypatch):
    # With no HERMES_WEBUI_EXTENSION_DIR and no managed default dir yet, the
    # gallery is "configured" (a managed install target is always available)
    # but not yet valid/enabled until the first install creates the directory.
    _use_extension_state_dir(monkeypatch, tmp_path)
    from api.extensions import get_extension_status

    assert get_extension_status() == {
        "enabled": False,
        "extension_dir_configured": True,
        "extension_dir_valid": False,
        "script_urls": [],
        "stylesheet_urls": [],
        "sidecars": [],
        "counts": _status_counts_empty(),
        "manifest": {
            "configured": False,
            "loaded": False,
            "status": "not_configured",
            "entry_count": 0,
            "script_count": 0,
            "stylesheet_count": 0,
            "sidecar_count": 0,
        },
        "extensions": [],
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
    assert status["counts"] == {"script_urls": 3, "stylesheet_urls": 2, "sidecars": 0, "manifest_extensions": 2, "user_disabled": 0}
    assert status["extensions"] == [
        {
            "id": "templates",
            "name": "templates",
            "manifest_enabled": True,
            "user_enabled": True,
            "user_disabled": False,
            "effective_enabled": True,
            "can_toggle": True,
            "reload_required": True,
            "status": "enabled",
        },
        {
            "id": "off",
            "name": "off",
            "manifest_enabled": False,
            "user_enabled": False,
            "user_disabled": False,
            "effective_enabled": False,
            "can_toggle": False,
            "reload_required": True,
            "status": "manifest_disabled",
        },
    ]
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


def test_extension_status_reports_manifest_disabled_when_dir_unconfigured(tmp_path, monkeypatch):
    # No managed default dir exists yet, so even though the gallery target is
    # "configured", a manifest env points at a directory that isn't valid yet.
    _use_extension_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["enabled"] is False
    assert status["extension_dir_configured"] is True
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




def test_extension_user_disabled_override_suppresses_manifest_assets_and_sidecars(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "desktop-companion",
              "name": "Desktop Companion",
              "scripts": ["companion.js"],
              "stylesheets": ["companion.css"],
              "sidecar": {"type": "loopback", "origin": "http://127.0.0.1:17787"}
            },
            {"id": "templates", "scripts": ["templates.js"]}
          ]
        }
        """,
        encoding="utf-8",
    )
    (state_dir / "extension-overrides.json").write_text(
        json.dumps({"version": 1, "disabled_extensions": ["desktop-companion"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_config, get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/templates.js"]
    assert status["stylesheet_urls"] == []
    assert status["sidecars"] == []
    assert status["manifest"]["script_count"] == 1
    assert status["manifest"]["stylesheet_count"] == 0
    assert status["manifest"]["sidecar_count"] == 0
    assert status["counts"]["manifest_extensions"] == 2
    assert status["counts"]["user_disabled"] == 1
    companion = next(item for item in status["extensions"] if item["id"] == "desktop-companion")
    assert companion["user_disabled"] is True
    assert companion["user_enabled"] is False
    assert companion["effective_enabled"] is False
    assert companion["can_toggle"] is True
    assert companion["status"] == "user_disabled"
    assert "17787" not in repr(status)

    config = get_extension_config()
    assert config["script_urls"] == ["/extensions/templates.js"]
    assert config["stylesheet_urls"] == []


def test_extension_state_invalid_file_fails_safe_without_path_leak(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates","scripts":["templates.js"]}]}',
        encoding="utf-8",
    )
    state_file = state_dir / "extension-overrides.json"
    state_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == ["/extensions/templates.js"]
    assert status["warnings"] == [
        {"code": "extension_state_unreadable", "source": "extension_state"}
    ]
    rendered = repr(status)
    assert str(state_file) not in rendered
    assert str(state_dir) not in rendered


def test_set_extension_user_enabled_persists_override_and_reenables(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates","scripts":["templates.js"]}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import set_extension_user_enabled

    disabled = set_extension_user_enabled("templates", False)
    assert disabled["script_urls"] == []
    assert disabled["counts"]["user_disabled"] == 1
    assert json.loads((state_dir / "extension-overrides.json").read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled_extensions": ["templates"],
    }

    enabled = set_extension_user_enabled("templates", True)
    assert enabled["script_urls"] == ["/extensions/templates.js"]
    assert enabled["counts"]["user_disabled"] == 0
    assert json.loads((state_dir / "extension-overrides.json").read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled_extensions": [],
    }


def test_set_extension_user_enabled_rejects_unknown_manifest_disabled_and_bad_shapes(tmp_path, monkeypatch):
    _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates"},{"id":"off","enabled":false}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import ExtensionToggleError, set_extension_user_enabled

    with pytest.raises(ExtensionToggleError) as bad_id:
        set_extension_user_enabled("../templates", False)
    assert bad_id.value.status == 400

    with pytest.raises(ExtensionToggleError) as bad_enabled:
        set_extension_user_enabled("templates", "false")
    assert bad_enabled.value.status == 400

    with pytest.raises(ExtensionToggleError) as unknown:
        set_extension_user_enabled("missing", False)
    assert unknown.value.status == 404

    with pytest.raises(ExtensionToggleError) as manifest_disabled_enable:
        set_extension_user_enabled("off", True)
    assert manifest_disabled_enable.value.status == 409

    with pytest.raises(ExtensionToggleError) as manifest_disabled_disable:
        set_extension_user_enabled("off", False)
    assert manifest_disabled_disable.value.status == 409



def test_extension_state_fails_safe_for_invalid_shapes_without_path_leak(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates","scripts":["templates.js"]}]}',
        encoding="utf-8",
    )
    state_file = state_dir / "extension-overrides.json"
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    cases = [
        (b"\xff\xfe", "extension_state_unreadable"),
        (json.dumps(["templates"]).encode("utf-8"), "extension_state_invalid"),
        (json.dumps({"disabled_extensions": "templates"}).encode("utf-8"), "extension_state_invalid"),
    ]
    for raw, warning_code in cases:
        state_file.write_bytes(raw)
        status = get_extension_status()
        assert status["script_urls"] == ["/extensions/templates.js"]
        assert {tuple(sorted(item.items())) for item in status["warnings"]} == {
            tuple(sorted({"code": warning_code, "source": "extension_state"}.items()))
        }
        rendered = repr(status)
        assert str(state_file) not in rendered
        assert str(state_dir) not in rendered


def test_extension_state_oversized_and_truncated_are_sanitized(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    entries = [
        {"id": f"ext-{index}", "scripts": [f"ext-{index}.js"] if index in (0, 511, 512) else []}
        for index in range(520)
    ]
    (root / "extensions.json").write_text(
        json.dumps({"extensions": entries}),
        encoding="utf-8",
    )
    state_file = state_dir / "extension-overrides.json"
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    state_file.write_text(" " * (32 * 1024 + 1), encoding="utf-8")
    oversized = get_extension_status()
    assert oversized["counts"]["user_disabled"] == 0
    assert oversized["warnings"] == [
        {"code": "extension_state_oversized", "source": "extension_state"}
    ]
    assert str(state_file) not in repr(oversized)

    state_file.write_text(
        json.dumps({"disabled_extensions": [f"ext-{index}" for index in range(520)]}),
        encoding="utf-8",
    )
    truncated = get_extension_status()
    assert truncated["counts"]["user_disabled"] == 512
    assert {tuple(sorted(item.items())) for item in truncated["warnings"]} == {
        tuple(sorted({"code": "extension_state_truncated", "source": "extension_state"}.items()))
    }
    assert "/extensions/ext-0.js" not in truncated["script_urls"]
    assert "/extensions/ext-511.js" not in truncated["script_urls"]
    assert "/extensions/ext-512.js" in truncated["script_urls"]


def test_extension_state_recursion_error_fails_safe(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    state_file = state_dir / "extension-overrides.json"
    state_file.write_text('{"disabled_extensions":["templates"]}', encoding="utf-8")

    import api.extensions as extensions

    def raise_recursion_error(_text):
        raise RecursionError("state nesting exceeded")

    monkeypatch.setattr(extensions.json, "loads", raise_recursion_error)
    state = extensions._load_extension_state({"warnings": []})
    assert state == {"version": 1, "disabled_extensions": []}

    diagnostics = {"warnings": []}
    extensions._load_extension_state(diagnostics)
    assert diagnostics["warnings"] == [
        {"code": "extension_state_unreadable", "source": "extension_state"}
    ]


def test_extension_state_invalid_entries_and_stale_ids_are_sanitized(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates","scripts":["templates.js"]}]}',
        encoding="utf-8",
    )
    (state_dir / "extension-overrides.json").write_text(
        json.dumps({"disabled_extensions": ["templates", "../bad", "missing"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import get_extension_status

    status = get_extension_status()
    assert status["script_urls"] == []
    assert status["counts"]["user_disabled"] == 1
    assert {tuple(sorted(item.items())) for item in status["warnings"]} == {
        tuple(sorted({"code": "extension_state_invalid_entries", "source": "extension_state"}.items())),
        tuple(sorted({"code": "extension_state_unknown_ids", "source": "extension_state"}.items())),
    }
    rendered = repr(status)
    assert "../bad" not in rendered
    assert "missing" not in rendered


def test_set_extension_user_enabled_is_idempotent_and_preserves_stale_ids(tmp_path, monkeypatch):
    state_dir = _use_extension_state_dir(monkeypatch, tmp_path)
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "extensions.json").write_text(
        '{"extensions":[{"id":"templates","scripts":["templates.js"]}]}',
        encoding="utf-8",
    )
    (state_dir / "extension-overrides.json").write_text(
        json.dumps({"version": 1, "disabled_extensions": ["stale"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")

    from api.extensions import set_extension_user_enabled

    set_extension_user_enabled("templates", False)
    set_extension_user_enabled("templates", False)
    assert json.loads((state_dir / "extension-overrides.json").read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled_extensions": ["stale", "templates"],
    }

    status = set_extension_user_enabled("templates", True)
    assert status["script_urls"] == ["/extensions/templates.js"]
    assert json.loads((state_dir / "extension-overrides.json").read_text(encoding="utf-8")) == {
        "version": 1,
        "disabled_extensions": ["stale"],
    }
    assert status["warnings"] == [
        {"code": "extension_state_unknown_ids", "source": "extension_state"}
    ]


def test_set_extension_user_enabled_rejects_when_extensions_unconfigured(monkeypatch):
    from api.extensions import ExtensionToggleError, set_extension_user_enabled

    with pytest.raises(ExtensionToggleError) as exc:
        set_extension_user_enabled("templates", False)
    assert exc.value.status == 404


def test_extension_toggle_route_uses_csrf_gate(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    from api import auth as auth_mod, routes

    auth_mod._invalidate_password_hash_cache()
    handler = FakeHandler()
    handler.headers = {
        "Origin": "http://example.com",
        "Host": "example.com",
        "Content-Length": "2",
    }

    result = routes.handle_post(handler, SimpleNamespace(path="/api/extensions/toggle"))
    assert result is None
    assert handler.status == 403
    assert json.loads(handler.body.decode("utf-8"))["error"] == "Session expired - reload the page"
    assert routes._csrf_exempt_path("/api/extensions/toggle") is False


def test_extension_toggle_route_requires_webui_auth(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api import auth as auth_mod
    from api.auth import check_auth

    auth_mod._invalidate_password_hash_cache()
    handler = FakeHandler()

    assert check_auth(handler, SimpleNamespace(path="/api/extensions/toggle", query="")) is False
    assert handler.status == 401
    assert handler.header("Location") is None
    assert json.loads(handler.body.decode("utf-8"))["error"] == "Authentication required"


def test_extension_toggle_route_is_wired_for_enable_disable(monkeypatch, tmp_path):
    from api import routes

    captured = {}

    def fake_j(handler, data, status=200, headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"id": "templates", "enabled": False})
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        "api.extensions.set_extension_user_enabled",
        lambda extension_id, enabled: {"ok": True, "id": extension_id, "enabled": enabled},
    )
    handler = FakeHandler()

    assert routes.handle_post(handler, SimpleNamespace(path="/api/extensions/toggle")) is True
    assert captured == {"status": 200, "data": {"ok": True, "id": "templates", "enabled": False}}


def test_extension_toggle_route_returns_sanitized_errors(monkeypatch):
    from api import routes
    from api.extensions import ExtensionToggleError

    captured = {}

    def fake_bad(handler, msg, status=400):
        captured["error"] = msg
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"id": "missing", "enabled": False})
    monkeypatch.setattr(routes, "bad", fake_bad)

    def raise_toggle_error(_extension_id, _enabled):
        raise ExtensionToggleError("Extension not found", status=404)

    monkeypatch.setattr("api.extensions.set_extension_user_enabled", raise_toggle_error)
    handler = FakeHandler()

    assert routes.handle_post(handler, SimpleNamespace(path="/api/extensions/toggle")) is True
    assert captured == {"error": "Extension not found", "status": 404}


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
