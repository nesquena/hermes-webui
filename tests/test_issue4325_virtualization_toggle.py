"""Regression coverage for transcript virtualization preference (#4325 + #4343 + #6151).

The stream-end freeze/jump fix (#4328, semantic viewport anchoring) is covered by
test_issue500_message_list_virtualization.py. This file covers the Preferences
toggle and its contract changes:

- #4325 added an opt-OUT toggle (default ON).
- #4343 flipped it to EXPERIMENTAL / opt-IN (default OFF) because virtualization
  caused a scroll-up flicker on long sessions, with a force-off-for-everyone
  migration: a stored virtualize_transcript=True from the #4325 window is reset
  to off unless an explicit post-flip opt-in marker (virtualize_transcript_optin)
  is present.
- #6151 re-enables DOM virtualization as the DEFAULT (ON) after the #4346
  Phase B footer-jitter fix resolved the scroll flicker root cause. The
  force-off migration and the opt-in marker are removed: the server defaults to
  True, an unset user gets ON, and a stored False (explicit opt-out) is honored.
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
    """#6151 default-ON contract: default True, bool-allowlisted, and the
    #4343 opt-in marker is gone (no force-off migration anymore)."""
    src = CONFIG.read_text(encoding="utf-8")
    assert '"virtualize_transcript": True' in src, "must default ON (#6151)"
    assert '"virtualize_transcript",' in src, "must be in _SETTINGS_BOOL_KEYS"
    assert "virtualize_transcript_optin" not in src, "opt-in marker must be removed with the force-off migration"


def test_settings_preferences_expose_virtualize_toggle_default_on():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsVirtualizeTranscript"' in html
    assert 'data-i18n="settings_label_virtualize_transcript"' in html
    assert 'data-i18n="settings_desc_virtualize_transcript"' in html
    # The checkbox is hydrated from the server value (!==false) on load, so the
    # static HTML must NOT pre-check it — a default-on user gets checked via JS.
    cb_line = next(l for l in html.splitlines() if 'id="settingsVirtualizeTranscript"' in l)
    assert "checked" not in cb_line, "default-on is applied by JS hydration, not a static checked attribute"


def test_boot_applies_saved_virtualize_preference_default_on():
    js = BOOT.read_text(encoding="utf-8")
    # #6151 default-on semantics: !==false (only an explicit false disables it).
    assert "window._virtualizeTranscript=s.virtualize_transcript!==false" in js
    # Settings-load-failed fallback mirrors the True config default.
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
    # #6151: the #4343 opt-in save marker is gone (no force-off migration).
    assert "payload.virtualize_transcript_optin" not in js
    # #6151: checkbox load reflects the default-on contract (!==false), so a
    # default-on user sees it checked and a saved opt-out shows unchecked.
    assert "virtualizeTranscriptCb.checked=settings.virtualize_transcript!==false;" in js
    assert "window._virtualizeTranscript=virtualizeTranscriptCb.checked;" in js
    # Hot-apply: toggling re-renders the open transcript immediately.
    assert "renderMessages({preserveScroll:true})" in js


def test_virtualize_toggle_i18n_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_virtualize_transcript:") == 15
    assert js.count("settings_desc_virtualize_transcript:") == 15


# ── #6151 default-on load behavior (load_settings) ──────────────────────────


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
    """No stored value (fresh / pre-#4325 install) → on (#6151 default)."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_true_is_honored(_settings_env):
    """A stored virtualize_transcript=True (from the #4325 window or a later
    explicit enable) is honored — with default-ON there is no force-off reset."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_false_optout_is_honored(_settings_env):
    """A stored False (explicit opt-out) stays off — the saved opt-out is never
    overridden by the default-ON contract."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": False})
    assert config.load_settings()["virtualize_transcript"] is False
