"""Pin the show_cli_sessions setting's default + hydration consistency (#3988).

show_cli_sessions defaults to True so that sessions from the TUI, Telegram,
Discord, CLI, and the Hermes One desktop app appear in the WebUI sidebar
without users having to discover the toggle in Settings (which was the source
of the surprise described in #3988).

This test pins the True default in every place the setting is read so a
future edit can't silently flip it back, or — worse — default it ON in
config.py while hydrating it OFF in the browser (the classic default-mismatch
bug from #4006, where an existing user with no saved value sees the feature
as disabled).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_show_cli_sessions_default_is_true_in_config():
    src = _read("api/config.py")
    assert re.search(r'["\']show_cli_sessions["\']\s*:\s*True', src), (
        "show_cli_sessions must default to True in _SETTINGS_DEFAULTS — "
        "CLI/TUI/messaging sessions visible by default (#3988)"
    )


def test_show_cli_sessions_in_bool_keys():
    src = _read("api/config.py")
    m = re.search(r"_SETTINGS_BOOL_KEYS\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    assert m, "_SETTINGS_BOOL_KEYS not found"
    assert "show_cli_sessions" in m.group(1), (
        "show_cli_sessions must be in _SETTINGS_BOOL_KEYS so it round-trips as a bool"
    )


def test_boot_hydration_defaults_true_when_setting_absent():
    """boot.js must hydrate _showCliSessions as True when the saved settings
    omit the key — `!!s.show_cli_sessions` would wrongly default it OFF for
    every existing user, contradicting the config.py default."""
    src = _read("static/boot.js")
    # Default-true read (=== false), not the truthy-coerce form.
    assert "window._showCliSessions=s.show_cli_sessions!==false" in src, (
        "boot.js must default _showCliSessions True when the saved value is absent"
    )
    assert "window._showCliSessions=!!s.show_cli_sessions" not in src, (
        "boot.js must not use !!s.show_cli_sessions — that defaults the True "
        "setting OFF for users with no saved value"
    )


def test_settings_checkbox_renders_checked_by_default():
    """The Settings checkbox must render checked when the setting is absent,
    matching the True default (panels.js settings-load)."""
    src = _read("static/panels.js")
    assert "showCliCb.checked=settings.show_cli_sessions!==false" in src, (
        "the show-CLI-sessions checkbox must default checked (=== false), not "
        "!!settings.show_cli_sessions which would render it unchecked by default"
    )
