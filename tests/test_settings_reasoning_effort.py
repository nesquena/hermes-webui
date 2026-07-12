"""Tests for the settings-panel default reasoning effort selector.

Verifies the HTML element exists, options are present, the selector is
initialised on settings load, and saveSettings() persists changes to the
/api/reasoning endpoint (writes agent.reasoning_effort to config.yaml).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def test_settings_reasoning_effort_select_exists():
    """A <select id="settingsReasoningEffort"> exists in index.html."""
    assert 'id="settingsReasoningEffort"' in INDEX_HTML


def test_settings_reasoning_effort_options_include_expected_levels():
    """The selector includes the canonical reasoning effort levels."""
    select_start = INDEX_HTML.index('id="settingsReasoningEffort"')
    select_chunk = INDEX_HTML[select_start:select_start + 800]
    for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        assert f'value="{level}"' in select_chunk, f"Missing option: {level}"


def test_settings_reasoning_effort_has_empty_default():
    """The selector has an empty-value '(Use provider default)' option."""
    select_start = INDEX_HTML.index('id="settingsReasoningEffort"')
    select_chunk = INDEX_HTML[select_start:select_start + 800]
    assert 'value=""' in select_chunk
    assert "Use provider default" in select_chunk


def test_save_settings_posts_reasoning_effort():
    """saveSettings() POSTs to /api/reasoning when reasoning effort changed."""
    func_start = PANELS_JS.index("async function saveSettings(")
    # saveSettings is ~200 lines — use a generous window
    save_chunk = PANELS_JS[func_start:func_start + 15000]
    assert "api('/api/reasoning'" in save_chunk, (
        "saveSettings must POST to /api/reasoning when reasoning effort changes"
    )
    assert "effort:" in save_chunk, (
        "POST body must include 'effort' key"
    )


def test_settings_reasoning_effort_compare_uses_on_open():
    """The change check compares against _settingsReasoningEffortOnOpen."""
    func_start = PANELS_JS.index("async function saveSettings(")
    save_chunk = PANELS_JS[func_start:func_start + 15000]
    assert "_settingsReasoningEffortOnOpen" in save_chunk, (
        "saveSettings must compare against _settingsReasoningEffortOnOpen"
    )


def test_load_settings_panel_fetches_reasoning_effort():
    """loadSettingsPanel() fetches /api/reasoning and populates the selector."""
    func_start = PANELS_JS.index("async function loadSettingsPanel()")
    # Search from function start onwards — function is ~600+ lines
    tail = PANELS_JS[func_start:]
    assert "api('/api/reasoning')" in tail, (
        "loadSettingsPanel must GET /api/reasoning to get current effort"
    )
    assert "_settingsReasoningEffortOnOpen" in tail, (
        "_settingsReasoningEffortOnOpen must be set on load"
    )
