"""
Regression coverage for model selector drift on fresh browser boot.

A stale browser-persisted model (localStorage) must not suppress the configured
profile/server default on page load. Restored sessions may still apply their own
session model later through loadSession().
"""
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def test_boot_settings_applies_default_even_when_browser_model_state_exists():
    assert "Fresh page boot must prefer the profile/server default" in BOOT_JS
    assert "_clearPersistedModelState" in BOOT_JS
    assert "localStorage.removeItem('hermes-webui-model-state')" in BOOT_JS
    assert "if(sel&&typeof _applyModelToDropdown==='function')" in BOOT_JS
    assert "if(sel&&!savedState&&typeof _applyModelToDropdown==='function')" not in BOOT_JS


def test_populate_model_dropdown_default_not_blocked_by_localstorage():
    assert "Do not let stale\n    // browser localStorage suppress the profile default" in UI_JS
    assert "if(data.default_model && !(S.session&&S.session.model))" in UI_JS
    assert "_readPersistedModelState()" not in _populate_default_guard_snippet()
    assert "localStorage.getItem('hermes-webui-model')" not in _populate_default_guard_snippet()


def _populate_default_guard_snippet() -> str:
    marker = "// Set default model from server on fresh/blank boot."
    start = UI_JS.index(marker)
    return UI_JS[start : start + 500]
