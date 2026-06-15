"""Pin the auto_scroll_follow setting's default + hydration consistency (#4006).

The viewport-follow default is `True` (Codex/Claude-Code-style sticky bottom:
follow new output to the bottom while streaming, but a deliberate scroll-up
unpins and is respected). This pins the default in every place it is read so a
future edit can't silently flip it or, worse, default it ON in config.py while
hydrating it OFF in the browser (the classic default-mismatch bug, where an
existing user with no saved value sees the feature as disabled).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_auto_scroll_follow_default_is_true_in_config():
    src = _read("api/config.py")
    assert re.search(r'["\']auto_scroll_follow["\']\s*:\s*True', src), (
        "auto_scroll_follow must default to True in _SETTINGS_DEFAULTS "
        "(sticky-bottom follow; scroll-up unpins)"
    )


def test_auto_scroll_follow_in_bool_keys():
    src = _read("api/config.py")
    m = re.search(r"_SETTINGS_BOOL_KEYS\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    assert m, "_SETTINGS_BOOL_KEYS not found"
    assert "auto_scroll_follow" in m.group(1), (
        "auto_scroll_follow must be in _SETTINGS_BOOL_KEYS so it round-trips as a bool"
    )


def test_boot_hydration_defaults_true_when_setting_absent():
    """boot.js must hydrate _autoScrollFollow as True when the saved settings
    omit the key — `!!s.auto_scroll_follow` would wrongly default it OFF for
    every existing user, contradicting the config.py default."""
    src = _read("static/boot.js")
    # Settings path: default-true read (=== false), not the truthy-coerce form.
    assert "window._autoScrollFollow=s.auto_scroll_follow!==false" in src, (
        "boot.js settings path must default _autoScrollFollow True when absent"
    )
    assert "window._autoScrollFollow=!!s.auto_scroll_follow" not in src, (
        "boot.js must not use !!s.auto_scroll_follow — that defaults the True "
        "setting OFF for users with no saved value"
    )
    # Fallback (no-settings) path must also default true.
    assert "window._autoScrollFollow=true" in src, (
        "boot.js fallback path must default _autoScrollFollow to true"
    )


def test_settings_checkbox_renders_checked_by_default():
    """The Appearance checkbox must render checked when the setting is absent,
    matching the True default (panels.js settings-load)."""
    src = _read("static/panels.js")
    assert "autoScrollFollowCb.checked=settings.auto_scroll_follow!==false" in src, (
        "the auto-follow checkbox must default checked (=== false), not "
        "!!settings.auto_scroll_follow which would render it unchecked by default"
    )


def test_follow_gate_references_auto_scroll_follow_and_unpin():
    """The DOM-replace follow gate must consult both _autoScrollFollow (the
    setting) and _messageUserUnpinned (the user's scroll-up), so the opt-out and
    the read-while-streaming behaviors both hold."""
    src = _read("static/ui.js")
    assert "_shouldFollowMessagesOnDomReplace" in src
    assert "_autoScrollFollow" in src and "_messageUserUnpinned" in src, (
        "the follow gate must reference both _autoScrollFollow and _messageUserUnpinned"
    )
