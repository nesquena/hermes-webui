"""Tests for #2841: show_cron_sessions toggle to surface cron sessions in the sidebar."""
import pathlib

from api.models import _hide_from_default_sidebar

ROOT = pathlib.Path(__file__).parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# --- _hide_from_default_sidebar behaviour ---

def test_cron_hidden_by_default():
    assert _hide_from_default_sidebar({'source_tag': 'cron', 'session_id': 'cron_abc'}) is True


def test_cron_visible_when_show_cron_true():
    assert _hide_from_default_sidebar({'source_tag': 'cron', 'session_id': 'cron_abc'}, show_cron=True) is False


def test_pre_compression_always_hidden_regardless_of_show_cron():
    assert _hide_from_default_sidebar({'pre_compression_snapshot': True}, show_cron=True) is True


def test_cron_hidden_with_explicit_false():
    assert _hide_from_default_sidebar({'source_tag': 'cron', 'session_id': 'cron_abc'}, show_cron=False) is True


# --- api/config.py string-scan ---

def test_show_cron_sessions_in_defaults():
    src = _read("api/config.py")
    assert '"show_cron_sessions": False' in src, (
        '"show_cron_sessions": False must appear in _SETTINGS_DEFAULTS'
    )


def test_show_cron_sessions_in_bool_keys():
    src = _read("api/config.py")
    assert '"show_cron_sessions"' in src, (
        '"show_cron_sessions" must appear in _SETTINGS_BOOL_KEYS'
    )
    # Verify it appears at least twice: once in _SETTINGS_DEFAULTS, once in _SETTINGS_BOOL_KEYS
    assert src.count('"show_cron_sessions"') >= 2, (
        '"show_cron_sessions" must appear in both _SETTINGS_DEFAULTS and _SETTINGS_BOOL_KEYS'
    )


# --- api/routes.py string-scan ---

def test_show_cron_sessions_kwarg_passthrough():
    src = _read("api/routes.py")
    assert "show_cron_sessions=show_cron_sessions" in src, (
        "show_cron_sessions kwarg must be forwarded at the _dedupe_cli_sidebar_sessions_for_api call site"
    )


# --- static/index.html string-scan ---

def test_settings_show_cron_sessions_in_html():
    src = _read("static/index.html")
    assert "settingsShowCronSessions" not in src, (
        "show_cron_sessions should no longer live in the Preferences panel"
    )


# --- static/panels.js string-scans ---

def test_panels_save_wiring():
    src = _read("static/panels.js")
    for token in [
        "settingsShowCliSessions",
        "settingsShowCronSessions",
        "settingsShowPreviousMessagingSessions",
    ]:
        assert token not in src, (
            "legacy sidebar filter settings should not remain in static/panels.js"
        )


def test_sidebar_filter_settings_persist_from_sessions_js():
    src = _read("static/sessions.js")
    assert "function _persistSidebarVisibilitySettings(next)" in src, (
        "sidebar filter persistence should live in static/sessions.js"
    )
    assert "show_cli_sessions: true" in src, (
        "sidebar filter persistence must keep non-WebUI sessions enabled"
    )
    assert "show_cron_sessions: true" in src, (
        "sidebar filter persistence must keep cron sessions enabled"
    )
    assert "function _toggleSidebarPreviousMessagingSessions(enabled)" in src, (
        "previous messaging visibility should be controlled from static/sessions.js"
    )
