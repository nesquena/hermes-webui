"""Tests for #2841: background session toggles surface hidden sessions in the sidebar."""
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


def test_webhook_hidden_by_default():
    assert _hide_from_default_sidebar({'source_tag': 'webhook', 'session_id': 'webhook:route:run'}) is True


def test_webhook_visible_when_show_webhook_true():
    assert _hide_from_default_sidebar(
        {'source_tag': 'webhook', 'session_id': 'webhook:route:run'},
        show_webhook=True,
    ) is False


def test_pre_compression_always_hidden_regardless_of_background_toggles():
    assert _hide_from_default_sidebar({'pre_compression_snapshot': True}, show_cron=True, show_webhook=True) is True


def test_cron_hidden_with_explicit_false():
    assert _hide_from_default_sidebar({'source_tag': 'cron', 'session_id': 'cron_abc'}, show_cron=False) is True


# --- api/config.py string-scan ---

def test_show_cron_sessions_in_defaults():
    src = _read("api/config.py")
    assert '"show_cron_sessions": False' in src, (
        '"show_cron_sessions": False must appear in _SETTINGS_DEFAULTS'
    )


def test_show_webhook_sessions_in_defaults():
    src = _read("api/config.py")
    assert '"show_webhook_sessions": False' in src, (
        '"show_webhook_sessions": False must appear in _SETTINGS_DEFAULTS'
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


def test_show_webhook_sessions_in_bool_keys():
    src = _read("api/config.py")
    assert '"show_webhook_sessions"' in src, (
        '"show_webhook_sessions" must appear in _SETTINGS_BOOL_KEYS'
    )
    assert src.count('"show_webhook_sessions"') >= 2, (
        '"show_webhook_sessions" must appear in both _SETTINGS_DEFAULTS and _SETTINGS_BOOL_KEYS'
    )


# --- api/routes.py string-scan ---

def test_show_cron_sessions_kwarg_passthrough():
    src = _read("api/routes.py")
    assert "show_cron_sessions=show_cron_sessions" in src, (
        "show_cron_sessions kwarg must be forwarded at the _dedupe_cli_sidebar_sessions_for_api call site"
    )


def test_show_webhook_sessions_kwarg_passthrough():
    src = _read("api/routes.py")
    assert "show_webhook_sessions=show_webhook_sessions" in src, (
        "show_webhook_sessions kwarg must be forwarded at the _dedupe_cli_sidebar_sessions_for_api call site"
    )


def test_show_webhook_sessions_invalidates_session_cache_on_settings_save():
    src = _read("api/routes.py")
    invalidation_block = src.split("Settings that change which sessions appear in the sidebar", 1)[1]
    invalidation_block = invalidation_block.split("auth_enabled_after", 1)[0]
    assert '"show_webhook_sessions"' in invalidation_block, (
        "settings POST must explicitly invalidate /api/sessions cache when show_webhook_sessions changes"
    )


# --- static/index.html string-scan ---

def test_settings_show_cron_sessions_in_html():
    src = _read("static/index.html")
    assert "settingsShowCronSessions" in src, (
        "settingsShowCronSessions checkbox must appear in static/index.html"
    )


def test_settings_show_webhook_sessions_in_html():
    src = _read("static/index.html")
    assert "settingsShowWebhookSessions" in src, (
        "settingsShowWebhookSessions checkbox must appear in static/index.html"
    )


# --- static/panels.js string-scans ---

def test_panels_save_wiring():
    src = _read("static/panels.js")
    # Both save paths (autosave _preferencesPayloadFromUi + explicit saveSettings)
    # must gate background sessions on the CLI-sessions checkbox so neither can
    # persist true while show_cli_sessions=false (#3514).
    assert "payload.show_cron_sessions=!!(showCliCb&&showCliCb.checked&&showCronCb.checked)" in src, (
        "autosave wiring must gate show_cron_sessions on settingsShowCliSessions in static/panels.js"
    )
    assert "body.show_cron_sessions=showCliSessions&&showCronSessions" in src, (
        "explicit saveSettings() must gate show_cron_sessions on showCliSessions in static/panels.js"
    )
    assert "payload.show_webhook_sessions=!!(showCliCb&&showCliCb.checked&&showWebhookCb.checked)" in src, (
        "autosave wiring must gate show_webhook_sessions on settingsShowCliSessions in static/panels.js"
    )
    assert "body.show_webhook_sessions=showCliSessions&&showWebhookSessions" in src, (
        "explicit saveSettings() must gate show_webhook_sessions on showCliSessions in static/panels.js"
    )


def test_panels_load_wiring():
    src = _read("static/panels.js")
    assert "show_cron_sessions" in src, (
        "load wiring for show_cron_sessions must appear in static/panels.js"
    )
    assert "show_webhook_sessions" in src, (
        "load wiring for show_webhook_sessions must appear in static/panels.js"
    )
