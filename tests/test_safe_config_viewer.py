"""Regression coverage for the safe config.yaml viewer (#2929)."""

from pathlib import Path
from typing import Any

import api.routes as routes

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_redact_config_masks_secret_key_paths_and_prefilters_plain_strings(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "_redact_text", lambda text: calls.append(text) or text.replace("ghp_sensitive", "[REDACTED]"))

    safe: dict[str, Any] = routes._redact_config_for_display({
        "providers": {"openai": {"api_key": "sk-live-secret", "model": "gpt-5.5"}},
        "gateway": {"api_key": 1234567890, "enabled": True},
        "platforms": {"telegram": {"token": False}},
        "webui": {"dashboard": {"public_url": "https://example.test"}},
        "notes": "contains ghp_sensitive token",
        "items": [{"password": "hunter2"}],
    })

    assert safe["providers"]["openai"]["api_key"] == "[REDACTED]"
    assert safe["gateway"]["api_key"] == "[REDACTED]"
    assert safe["gateway"]["enabled"] is True
    assert safe["platforms"]["telegram"]["token"] == "[REDACTED]"
    assert safe["providers"]["openai"]["model"] == "gpt-5.5"
    assert safe["webui"]["dashboard"]["public_url"] == "https://example.test"
    assert safe["notes"] == "contains [REDACTED] token"
    assert safe["items"][0]["password"] == "[REDACTED]"
    assert "sk-live-secret" not in calls


def test_safe_config_endpoint_is_get_only_read_only_and_uses_active_config_path():
    assert '"/api/config/safe"' in ROUTES_PY
    endpoint_idx = ROUTES_PY.index('if parsed.path == "/api/config/safe"')
    settings_idx = ROUTES_PY.index('"/api/settings"', endpoint_idx)
    block = ROUTES_PY[endpoint_idx:settings_idx]
    assert "_safe_config_yaml_text()" in block
    assert "_get_config_path()" in block
    assert '"path": str(cfg_path)' not in block
    assert '"read_only": True' in block
    assert 'if parsed.path == "/api/config/safe"' in block


def test_system_settings_mounts_read_only_safe_config_viewer():
    assert 'id="safeConfigText"' in INDEX_HTML
    assert 'onclick="loadSafeConfig(true)"' in INDEX_HTML
    assert 'onclick="copySafeConfig()"' in INDEX_HTML
    assert "read-only" in INDEX_HTML
    assert ".safe-config-viewer" in STYLE_CSS


def test_safe_config_frontend_loads_and_copies_redacted_yaml():
    assert "async function loadSafeConfig" in PANELS_JS
    assert "api('/api/config/safe')" in PANELS_JS
    assert "loadSafeConfig();" in PANELS_JS
    assert "async function copySafeConfig" in PANELS_JS
    assert "navigator.clipboard.writeText" in PANELS_JS


def test_safe_config_i18n_and_changelog_entries_exist():
    for key in [
        "safe_config_title",
        "safe_config_desc",
        "safe_config_refresh",
        "safe_config_copy",
        "safe_config_meta",
        "safe_config_copied",
    ]:
        assert key in I18N_JS
    assert "safe, read-only config.yaml viewer" in CHANGELOG
    assert "#2929" in CHANGELOG
