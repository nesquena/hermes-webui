"""Regression coverage for transcript virtualization preference (#4325 → #4343 → default-ON).

The stream-end freeze/jump fix (#4328, semantic viewport anchoring) is covered by
test_issue500_message_list_virtualization.py. This file covers the Preferences
toggle and its default-ON contract:

- #4325 added an opt-OUT toggle (default ON).
- #4343 flipped it to EXPERIMENTAL / opt-IN (default OFF) due to scroll-up flicker,
  with a force-off-for-everyone migration.
- Default-ON restoration: #4346 Phase B resolved the flicker root cause (footer-jitter
  suppression during virtual-scroll measurement). Virtualization is now default ON
  again. The #4343 force-off migration has been retired — stored values are respected
  as-is. Users who explicitly set False keep their opt-out; new users get True.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
UI = REPO_ROOT / "static" / "ui.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_virtualize_transcript_setting_is_default_on_and_allowed():
    """Default-ON model: default True (virtualization on), bool-allowlisted,
    plus the legacy opt-in marker retained for backward compatibility."""
    src = CONFIG.read_text(encoding="utf-8")
    assert '"virtualize_transcript": True' in src, "must default ON"
    assert '"virtualize_transcript",' in src, "must be in _SETTINGS_BOOL_KEYS"
    assert '"virtualize_transcript_optin": False' in src, "legacy opt-in marker must exist + default False"
    assert '"virtualize_transcript_optin",' in src, "opt-in marker must be in _SETTINGS_BOOL_KEYS"


def test_settings_preferences_expose_virtualize_toggle():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsVirtualizeTranscript"' in html
    assert 'data-i18n="settings_label_virtualize_transcript"' in html
    assert 'data-i18n="settings_desc_virtualize_transcript"' in html


def test_boot_applies_saved_virtualize_preference_default_on():
    js = BOOT.read_text(encoding="utf-8")
    # Default-on semantics: !==false (anything except an explicit false enables it).
    assert "window._virtualizeTranscript=s.virtualize_transcript!==false" in js
    # Settings-load-failed fallback also defaults ON.
    assert "window._virtualizeTranscript=true" in js


def test_ui_gate_forces_full_render_when_disabled():
    js = UI.read_text(encoding="utf-8")
    start = js.index("function _currentMessageVirtualWindow(")
    body = js[start:start + 900]
    assert "_virtualizeTranscript===false" in body
    assert "virtualized:false" in body


def test_panels_round_trip_and_hot_apply_virtualize_toggle():
    js = PANELS.read_text(encoding="utf-8")
    assert "const virtualizeTranscriptCb=$('settingsVirtualizeTranscript');" in js
    assert "payload.virtualize_transcript=virtualizeTranscriptCb.checked;" in js
    # Default-on: checkbox load honors anything except an explicit false (!==false).
    assert "virtualizeTranscriptCb.checked=settings.virtualize_transcript!==false;" in js
    assert "window._virtualizeTranscript=virtualizeTranscriptCb.checked;" in js
    # Hot-apply: toggling re-renders the open transcript immediately.
    assert "renderMessages({preserveScroll:true})" in js


def test_virtualize_toggle_i18n_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_virtualize_transcript:") == 15
    assert js.count("settings_desc_virtualize_transcript:") == 15


def test_virtualize_toggle_i18n_no_experimental_language():
    """The label and description must not contain 'experimental' language
    now that virtualization is a stable default-on feature."""
    js = I18N.read_text(encoding="utf-8")
    # Extract all virtualize_transcript label/desc lines
    lines = [l for l in js.splitlines() if 'virtualize_transcript' in l.lower()
             and ('settings_label' in l or 'settings_desc' in l)]
    for line in lines:
        lower = line.lower()
        assert 'experimental' not in lower, f"experimental language found: {line.strip()}"
        assert 'expérimental' not in lower, f"experimental language found: {line.strip()}"
        assert 'experimentální' not in lower, f"experimental language found: {line.strip()}"
        assert '実験的' not in lower, f"experimental language found: {line.strip()}"
        assert 'thử nghiệm' not in lower, f"experimental language found: {line.strip()}"


# ── load_settings behavior (force-off migration retired) ──────────────────


@pytest.fixture
def _settings_env(tmp_path, monkeypatch):
    """Point load_settings at an isolated settings.json under tmp."""
    import api.config as config

    sf = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    return config, sf


def _write(sf, payload):
    sf.write_text(json.dumps(payload), encoding="utf-8")


def test_migration_unset_defaults_on(_settings_env):
    """No stored value (fresh install) → ON (new default)."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_true_is_honored(_settings_env):
    """A stored virtualize_transcript=True is honored (no force-off reset).
    The #4343 force-off migration has been retired."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_false_is_honored(_settings_env):
    """A stored virtualize_transcript=False (explicit opt-out) is respected."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": False})
    assert config.load_settings()["virtualize_transcript"] is False


def test_migration_optin_marker_irrelevant(_settings_env):
    """The opt-in marker no longer affects the outcome — stored value is
    respected regardless of whether the marker is present."""
    config, sf = _settings_env
    # True without marker → True (was force-reset to False under #4343)
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": True})
    assert config.load_settings()["virtualize_transcript"] is True
    # False with marker → False (explicit opt-out)
    _write(sf, {
        "onboarding_completed": True,
        "virtualize_transcript": False,
        "virtualize_transcript_optin": True,
    })
    assert config.load_settings()["virtualize_transcript"] is False
