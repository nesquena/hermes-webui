"""Focused regression coverage for #5145 busy-input defaults."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_backend_default_resolves_to_steer():
    assert '"busy_input_mode": "steer"' in CONFIG_PY


def test_boot_defaults_resolve_to_steer():
    assert "window._busyInputMode=(s.busy_input_mode||'steer')" in BOOT_JS
    assert "window._busyInputMode='steer'" in BOOT_JS


def test_settings_panel_fallbacks_resolve_to_steer():
    assert "String(settings.busy_input_mode||'steer')" in PANELS_JS
    assert "['queue','interrupt','steer'].includes(val)?val:'steer'" in PANELS_JS
    assert "window._busyInputMode=body.busy_input_mode||'steer'" in PANELS_JS


def test_busy_input_label_changes_without_key_or_id_drift():
    assert 'id="settingsBusyInputMode"' in INDEX_HTML
    assert 'data-i18n="settings_label_busy_input_mode">While agent is running' in INDEX_HTML
    assert "settings_label_busy_input_mode: 'While agent is running'" in I18N_JS
