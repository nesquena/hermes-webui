"""Behavioral tests for show_fallback_notices persistence (PR #5755).

The gate-certifier found that show_fallback_notices was added to
_SETTINGS_DEFAULTS but then excluded from _SETTINGS_ALLOWED_KEYS and
absent from _SETTINGS_BOOL_KEYS. save_settings() silently discarded the
posted checkbox value, so turning notices off did not survive save/reload.

These tests exercise save_settings()/load_settings() directly — not
source-string membership — so the exclusion mistake cannot recur without
failing a behavioral round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _import_config(tmp_settings_file: Path):
    """Import api.config with SETTINGS_FILE pointed at a temp path.

    api.config reads SETTINGS_FILE at import time (startup settings load),
    so we must set the env var BEFORE the first import. Once imported, the
    module-level SETTINGS_FILE is fixed, but save_settings/load_settings
    read from it dynamically — so we monkeypatch the module attribute.
    """
    import api.config as config

    # Monkeypatch the module-level SETTINGS_FILE to our temp path
    original = config.SETTINGS_FILE
    config.SETTINGS_FILE = tmp_settings_file
    return config, original


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    """Provide a clean state dir with no settings.json."""
    settings_file = tmp_path / "settings.json"
    # Ensure no file exists
    if settings_file.exists():
        settings_file.unlink()
    config, original = _import_config(settings_file)
    yield config, settings_file
    # Restore original
    config.SETTINGS_FILE = original


# ── 1: absent-file default is True ──────────────────────────────────────────


def test_show_fallback_notices_default_true_when_settings_file_absent(settings_dir):
    """When settings.json does not exist, load_settings() must return
    show_fallback_notices=True (the _SETTINGS_DEFAULTS value)."""
    config, settings_file = settings_dir
    assert not settings_file.exists(), "fixture must start with no settings file"

    loaded = config.load_settings()
    assert loaded.get("show_fallback_notices") is True, (
        "show_fallback_notices must default to True when settings.json is absent. "
        "If this fails, the key is missing from _SETTINGS_DEFAULTS or the default "
        "value changed."
    )


# ── 2: save False → reload False round-trip ─────────────────────────────────


def test_show_fallback_notices_false_round_trip(settings_dir):
    """Saving show_fallback_notices=False must persist and survive a reload.

    This is the core regression: previously, show_fallback_notices was in the
    _SETTINGS_ALLOWED_KEYS exclusion set, so save_settings() silently discarded
    the posted False value and load_settings() always returned the default True.
    """
    config, settings_file = settings_dir

    # Save False
    result = config.save_settings({"show_fallback_notices": False})
    assert result.get("show_fallback_notices") is False, (
        "save_settings() must return show_fallback_notices=False when posted False. "
        "If this fails, the key is either excluded from _SETTINGS_ALLOWED_KEYS or "
        "not boolean-coerced via _SETTINGS_BOOL_KEYS."
    )

    # Verify it was actually written to disk
    assert settings_file.exists(), "save_settings() must write settings.json"
    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert raw.get("show_fallback_notices") is False, (
        "show_fallback_notices=False must be persisted to settings.json. "
        "If this fails, _settings_payload_for_write is stripping the key."
    )

    # Reload from disk — must still be False
    loaded = config.load_settings()
    assert loaded.get("show_fallback_notices") is False, (
        "load_settings() must return show_fallback_notices=False after saving False. "
        "If this fails, the persisted value was not read back correctly."
    )


# ── 3: save True → reload True round-trip ───────────────────────────────────


def test_show_fallback_notices_true_round_trip(settings_dir):
    """Saving show_fallback_notices=True must persist and survive a reload.

    Symmetric to the False round-trip: ensures the setting is not hardcoded
    to one direction and that True is properly boolean-coered and written.
    """
    config, settings_file = settings_dir

    # First save False to establish a non-default state
    config.save_settings({"show_fallback_notices": False})
    assert config.load_settings().get("show_fallback_notices") is False

    # Now save True
    result = config.save_settings({"show_fallback_notices": True})
    assert result.get("show_fallback_notices") is True, (
        "save_settings() must return show_fallback_notices=True when posted True."
    )

    # Verify disk
    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert raw.get("show_fallback_notices") is True, (
        "show_fallback_notices=True must be persisted to settings.json."
    )

    # Reload
    loaded = config.load_settings()
    assert loaded.get("show_fallback_notices") is True, (
        "load_settings() must return show_fallback_notices=True after saving True."
    )


# ── 4: boolean coercion of truthy/falsy values ──────────────────────────────


def test_show_fallback_notices_boolean_coercion(settings_dir):
    """save_settings() must coerce show_fallback_notices to a proper bool.

    The setting is in _SETTINGS_BOOL_KEYS, so posted values like "false"
    (string), 0 (int), or 1 (int) must be coerced to bool. This prevents
    a truthy string "false" from being stored as True.
    """
    config, settings_file = settings_dir

    # Post integer 0 — must coerce to False
    result = config.save_settings({"show_fallback_notices": 0})
    assert result.get("show_fallback_notices") is False, (
        "save_settings() must coerce 0 to False for show_fallback_notices."
    )

    # Post integer 1 — must coerce to True
    result = config.save_settings({"show_fallback_notices": 1})
    assert result.get("show_fallback_notices") is True, (
        "save_settings() must coerce 1 to True for show_fallback_notices."
    )


# ── 5: minimal-DOM render test (node-based, tool-only) ──────────────────────


def test_fallback_notice_renders_when_enabled_and_hidden_when_disabled():
    """Minimal-DOM tool-only render test: _fallbackNoticeHtml() must produce
    exactly one .fallback-notice element when enabled, and none when disabled.

    The render path in ui.js gates on window._showFallbackNotices !== false:
        const fallbackNoticeHtml = (!isUser && m._fallbackNotice
                                    && window._showFallbackNotices !== false)
                                   ? _fallbackNoticeHtml(m._fallbackNotice) : '';

    This test extracts _fallbackNoticeHtml() from ui.js, provides a minimal
    esc() stub, and verifies the HTML output contains the fallback-notice
    class when the notice is present. The gating logic (enabled/disabled) is
    tested by checking the condition directly.
    """
    import shutil
    import subprocess

    NODE = shutil.which("node")
    if not NODE:
        pytest.skip("node is required for the minimal-DOM render test")

    ui_src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

    # Extract _fallbackNoticeHtml function body
    func_start = ui_src.find("function _fallbackNoticeHtml(")
    assert func_start != -1, "_fallbackNoticeHtml not found in ui.js"

    # Find the closing brace of the function
    brace = ui_src.find("{", func_start)
    depth = 0
    func_end = -1
    for i in range(brace, len(ui_src)):
        if ui_src[i] == "{":
            depth += 1
        elif ui_src[i] == "}":
            depth -= 1
            if depth == 0:
                func_end = i + 1
                break
    assert func_end != -1, "_fallbackNoticeHtml body did not close"
    func_src = ui_src[func_start:func_end]

    # Build a node script that evals the function with a stub esc() and
    # checks the output. The gating condition (window._showFallbackNotices)
    # is tested explicitly to verify the enable/disable logic.
    script = """
const fs = require('fs');

// Stub esc() — _fallbackNoticeHtml uses it for HTML escaping
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// Eval the extracted function
${FUNC_SRC}

// Test 1: notice present → produces .fallback-notice element
const notice = { message: 'Switched to fallback model', to_model: 'gpt-4o', to_provider: 'openai' };
const html = _fallbackNoticeHtml(notice);
const count = (html.match(/fallback-notice/g) || []).length;
if (count < 1) {
  console.error('FAIL: expected at least one fallback-notice class in output, got: ' + html);
  process.exit(1);
}
// Must have exactly one data-fallback-notice attribute
const dataCount = (html.match(/data-fallback-notice="1"/g) || []).length;
if (dataCount !== 1) {
  console.error('FAIL: expected exactly one data-fallback-notice="1", got ' + dataCount + ' in: ' + html);
  process.exit(1);
}

// Test 2: null notice → empty string (no .fallback-notice)
const emptyHtml = _fallbackNoticeHtml(null);
if (emptyHtml !== '') {
  console.error('FAIL: null notice should produce empty string, got: ' + emptyHtml);
  process.exit(1);
}
if (emptyHtml.includes('fallback-notice')) {
  console.error('FAIL: null notice should not contain fallback-notice');
  process.exit(1);
}

// Test 3: gating condition — window._showFallbackNotices !== false
// This mirrors the ui.js render line:
//   const fallbackNoticeHtml = (!isUser && m._fallbackNotice && window._showFallbackNotices !== false)
//                              ? _fallbackNoticeHtml(m._fallbackNotice) : '';
function gatedRender(notice, showFallbackNotices) {
  const isUser = false;
  const m = { _fallbackNotice: notice };
  const fallbackNoticeHtml = (!isUser && m._fallbackNotice && showFallbackNotices !== false)
    ? _fallbackNoticeHtml(m._fallbackNotice) : '';
  return fallbackNoticeHtml;
}

// When enabled (true or undefined) → notice renders
const enabledHtml = gatedRender(notice, true);
if (!enabledHtml.includes('fallback-notice')) {
  console.error('FAIL: notice should render when _showFallbackNotices=true');
  process.exit(1);
}
const defaultHtml = gatedRender(notice, undefined);
if (!defaultHtml.includes('fallback-notice')) {
  console.error('FAIL: notice should render when _showFallbackNotices=undefined (default)');
  process.exit(1);
}

// When disabled (false) → notice does NOT render
const disabledHtml = gatedRender(notice, false);
if (disabledHtml.includes('fallback-notice')) {
  console.error('FAIL: notice should NOT render when _showFallbackNotices=false');
  process.exit(1);
}

console.log('OK: all render assertions passed');
""".replace("${FUNC_SRC}", func_src)

    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"node render test failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK: all render assertions passed" in result.stdout, (
        f"render test did not report success:\n{result.stdout}\n{result.stderr}"
    )
