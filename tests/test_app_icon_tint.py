"""App-icon tint settings and PWA integration."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class _FakeHandler:
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

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


def _get(path):
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urlparse(f"http://example.com{path}"))
    return handler


def test_icon_tint_is_a_validated_appearance_setting(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    assert config._SETTINGS_DEFAULTS["icon_tint"] == "#08EBF1"
    assert "icon_tint" in config._SETTINGS_HEX_COLOR_KEYS

    saved = config.save_settings({"icon_tint": "#e5484d"})
    assert saved["icon_tint"] == "#E5484D"

    saved = config.save_settings({"icon_tint": "red; background: url(evil)"})
    assert saved["icon_tint"] == "#E5484D"


def test_tinted_favicon_route_uses_requested_color():
    handler = _get("/static/favicon.svg?tint=E5484D")

    assert handler.status == 200
    assert handler.header("Content-Type").startswith("image/svg+xml")
    assert handler.header("Cache-Control") == "no-store"
    assert "#E5484D" in bytes(handler.body).decode("utf-8")

    session_handler = _get("/session/static/favicon.svg?tint=7C3AED")
    assert session_handler.status == 200
    assert "#7C3AED" in bytes(session_handler.body).decode("utf-8")


def test_tinted_favicon_handles_source_gradient_color_without_collision():
    handler = _get("/static/favicon.svg?tint=3889FD")
    svg = bytes(handler.body).decode("utf-8")

    assert handler.status == 200
    assert svg.count('stop-color="#3889FD"') == 1
    assert svg.count('stop-color="#2760B1"') == 1


def test_manifest_points_install_icons_at_current_tint(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "load_settings", lambda: {"icon_tint": "#E5484D"})
    handler = _get("/manifest.json")
    manifest = json.loads(bytes(handler.body).decode("utf-8"))

    assert handler.status == 200
    assert manifest["icons"]
    assert all(
        icon["src"].endswith("favicon.svg?tint=E5484D") for icon in manifest["icons"]
    )
    assert all(icon["type"] == "image/svg+xml" for icon in manifest["icons"])
    assert manifest["shortcuts"][0]["icons"][0]["src"].endswith(
        "favicon.svg?tint=E5484D"
    )


def test_icon_tint_control_updates_favicon_and_autosaves():
    assert 'id="settingsIconTint"' in INDEX
    assert (
        'rel="apple-touch-icon" sizes="512x512" href="static/apple-touch-icon.png"'
        in INDEX
    )
    assert "function _pickIconTint(" in BOOT
    assert "_applyIconTint" in BOOT
    assert (
        "_scheduleAppearanceAutosave()" in BOOT[BOOT.index("function _pickIconTint(") :]
    )
    assert "icon_tint:" in PANELS[PANELS.index("function _appearancePayloadFromUi(") :]
    assert "body.icon_tint=iconTint;" in PANELS.replace(" ", "")


def test_tinted_icon_routes_are_public_when_auth_is_enabled(monkeypatch):
    from api.auth import _invalidate_password_hash_cache, check_auth

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    _invalidate_password_hash_cache()
    try:
        assert (
            check_auth(
                _FakeHandler(),
                SimpleNamespace(path="/static/favicon.svg", query="tint=E5484D"),
            )
            is True
        )
        assert (
            check_auth(
                _FakeHandler(),
                SimpleNamespace(
                    path="/session/static/favicon.svg", query="tint=E5484D"
                ),
            )
            is True
        )
    finally:
        monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
        _invalidate_password_hash_cache()
