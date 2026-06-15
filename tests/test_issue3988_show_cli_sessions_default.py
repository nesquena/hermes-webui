"""show_cli_sessions default-True + grandfather migration + hydration consistency (#3988).

The default for ``show_cli_sessions`` flipped to **True** so that NEW installs
surface CLI / TUI / Telegram / Discord / Hermes One sessions in the WebUI sidebar
without users having to discover the toggle in Settings (the surprise described in
#3988).

Two correctness properties this pins:

1. **Grandfather established installs OFF.** An existing user who already completed
   onboarding and never opted in must NOT have their sidebar silently change. The
   ``load_settings()`` backfill pins ``show_cli_sessions=False`` when the saved
   settings predate the flip (``onboarding_completed`` True, key absent). Fresh
   installs (no settings file / onboarding not completed) get the True default.

2. **Default-true hydration at EVERY read site.** config.py, boot.js, and the
   panels.js Settings checkbox must all default an *absent* value to True — a
   ``!!value`` coerce would default the True setting OFF for a user with no saved
   value (the #4006 default-mismatch class), showing config-ON-but-UI-OFF.

Combines + supersedes the approaches in #3997 (rodboev) and #4222 (Sanjays2402).
"""

import json
import pathlib

import pytest

import api.config as config

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 1. config.py default ──────────────────────────────────────────────────

def test_show_cli_sessions_default_is_true_in_config():
    assert config._SETTINGS_DEFAULTS.get("show_cli_sessions") is True, (
        "show_cli_sessions must default to True so CLI/TUI/messaging sessions are "
        "visible by default for new installs (#3988)"
    )


def test_show_cli_sessions_in_bool_keys():
    assert "show_cli_sessions" in config._SETTINGS_BOOL_KEYS, (
        "show_cli_sessions must be in _SETTINGS_BOOL_KEYS so it round-trips as a bool"
    )


# ── 2. Grandfather migration in load_settings() ───────────────────────────

@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    """Point config.SETTINGS_FILE at a temp file for isolated load_settings tests."""
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    return f


def _write(f, data):
    f.write_text(json.dumps(data), encoding="utf-8")


def test_fresh_install_no_settings_file_defaults_on(settings_file):
    """No settings file at all (brand-new install) → True (the new default)."""
    assert not settings_file.exists()
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is True


def test_fresh_install_onboarding_not_completed_defaults_on(settings_file):
    """Settings file with ONLY a not-yet-completed onboarding flag (still
    mid-first-run, no other user state) → True."""
    _write(settings_file, {"onboarding_completed": False})
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is True


def test_established_cli_user_without_onboarding_flag_grandfathered_off(settings_file):
    """A CLI-configured user who tweaked a WebUI setting (e.g. theme) but never
    ran the onboarding wizard is still an ESTABLISHED install — grandfather OFF.

    Keying the grandfather only on onboarding_completed would miss this user and
    silently flip their sidebar on (Opus #4230 review finding). Any persisted
    setting other than a falsy onboarding flag marks the install established.
    """
    _write(settings_file, {"onboarding_completed": False, "theme": "dark"})
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is False, (
        "an install with persisted user settings (even without onboarding "
        "completed) must be grandfathered OFF, not flipped on"
    )


def test_established_install_absent_key_grandfathered_off(settings_file):
    """Established install (onboarding done) that never set the key → stays OFF.

    This is the core grandfather guarantee: an existing user's sidebar must not
    silently start showing CLI/messaging sessions after the default flips.
    """
    _write(settings_file, {"onboarding_completed": True, "theme": "dark"})
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is False, (
        "an onboarding-completed install with no saved show_cli_sessions must be "
        "grandfathered OFF, not flipped on by the new default"
    )


def test_explicit_true_is_respected(settings_file):
    """A user who explicitly opted in keeps it ON."""
    _write(settings_file, {"onboarding_completed": True, "show_cli_sessions": True})
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is True


def test_explicit_false_is_respected(settings_file):
    """A user who explicitly turned it off keeps it OFF."""
    _write(settings_file, {"onboarding_completed": True, "show_cli_sessions": False})
    loaded = config.load_settings()
    assert loaded["show_cli_sessions"] is False


def test_established_install_persists_grandfather_through_save(settings_file):
    """save_settings() does load_settings() first, so the grandfathered False is
    carried into the persisted blob — no window where a save flips an established
    user ON. (save_settings writes the full merged dict.)"""
    _write(settings_file, {"onboarding_completed": True})
    # A save that doesn't mention show_cli_sessions must not flip it on.
    config.save_settings({"theme": "light"})
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted.get("show_cli_sessions") is False, (
        "save_settings must preserve the grandfathered OFF state for established "
        "installs, never silently flip it on"
    )


# ── 3. Default-true hydration at every frontend read site ──────────────────
#    (string-pins; guard against the #4006 !!-coerce default-mismatch class)

def test_boot_hydration_defaults_true_when_setting_absent():
    src = _read("static/boot.js")
    assert "window._showCliSessions=s.show_cli_sessions!==false" in src, (
        "boot.js must default _showCliSessions True when the saved value is absent"
    )
    assert "window._showCliSessions=!!s.show_cli_sessions" not in src, (
        "boot.js must not use !!s.show_cli_sessions — that defaults the True "
        "setting OFF for users with no saved value (#4006 mismatch class)"
    )


def test_boot_settings_load_failure_fallback_defaults_true():
    """The settings-load-FAILED catch block must also default _showCliSessions
    True, mirroring the config default — otherwise a transient settings-read
    error silently hides CLI sessions (the #4006 catch-block-fallback class the
    autoScrollFollow fix pinned)."""
    src = _read("static/boot.js")
    assert "window._showCliSessions=false" not in src, (
        "the settings-load-failure fallback must not hardcode _showCliSessions "
        "false — it should mirror the True default"
    )


def test_settings_checkbox_renders_checked_by_default():
    src = _read("static/panels.js")
    assert "showCliCb.checked=settings.show_cli_sessions!==false" in src, (
        "the show-CLI-sessions checkbox must default checked (!== false), matching "
        "the True config default"
    )
    assert "showCliCb.checked=!!settings.show_cli_sessions" not in src, (
        "panels.js must not use !!settings.show_cli_sessions for the checkbox — "
        "that renders it unchecked by default, contradicting the config default"
    )
