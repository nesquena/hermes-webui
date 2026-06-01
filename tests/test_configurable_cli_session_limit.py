"""Regression checks for configurable non-WebUI sidebar session limits."""

import json
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
MODELS_PY = (ROOT / "api" / "models.py").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_cli_session_limit_setting_is_exposed_and_wired():
    assert '"cli_session_limit": 20' in CONFIG_PY
    assert '"cli_session_limit": (1, 500)' in CONFIG_PY
    assert "def _cli_visible_session_limit()" in MODELS_PY
    assert "limit=_cli_visible_session_limit()" in MODELS_PY
    assert "def _resolve_cli_session_cap(" in ROUTES_PY
    assert "cli_cap=_resolve_cli_session_cap(settings)" in ROUTES_PY
    assert 'id="settingsCliSessionLimit"' in INDEX_HTML
    assert 'min="1"' in INDEX_HTML
    assert 'max="500"' in INDEX_HTML
    assert "payload.cli_session_limit=parseInt(cliLimitField.value,10)" in PANELS_JS
    assert "settings.cli_session_limit" in PANELS_JS
    assert "window._cliSessionLimit=parseInt(s.cli_session_limit||20,10)||20" in BOOT_JS


def test_settings_api_persists_cli_session_limit_and_rejects_invalid_values():
    try:
        d, status = post("/api/settings", {"cli_session_limit": 200})
        assert status == 200
        assert d["cli_session_limit"] == 200

        d, status = post("/api/settings", {"cli_session_limit": "150"})
        assert status == 200
        assert d["cli_session_limit"] == 150

        d, status = post("/api/settings", {"cli_session_limit": 0})
        assert status == 200
        assert d["cli_session_limit"] == 150

        d, status = post("/api/settings", {"cli_session_limit": 9999})
        assert status == 200
        assert d["cli_session_limit"] == 150
    finally:
        post("/api/settings", {"cli_session_limit": 20})
