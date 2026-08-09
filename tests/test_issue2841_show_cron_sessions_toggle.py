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


def test_kanban_hidden_by_default():
    assert _hide_from_default_sidebar({'source_tag': 'kanban', 'session_id': 'kanban_worker_1'}) is True


def test_kanban_visible_when_show_kanban_true():
    assert _hide_from_default_sidebar(
        {'source_tag': 'kanban', 'session_id': 'kanban_worker_1'},
        show_kanban=True,
    ) is False


def test_kanban_hidden_with_explicit_false():
    assert _hide_from_default_sidebar({'source_tag': 'kanban', 'session_id': 'kanban_worker_1'}, show_kanban=False) is True


def test_kanban_does_not_leak_when_other_toggles_enabled():
    assert _hide_from_default_sidebar(
        {'source_tag': 'kanban', 'session_id': 'kanban_worker_1'},
        show_cron=True,
        show_webhook=True,
    ) is True


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


def test_show_kanban_sessions_in_defaults():
    src = _read("api/config.py")
    assert '"show_kanban_sessions": False' in src, (
        '"show_kanban_sessions": False must appear in _SETTINGS_DEFAULTS'
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


def test_show_kanban_sessions_in_bool_keys():
    src = _read("api/config.py")
    assert '"show_kanban_sessions"' in src, (
        '"show_kanban_sessions" must appear in _SETTINGS_BOOL_KEYS'
    )
    assert src.count('"show_kanban_sessions"') >= 2, (
        '"show_kanban_sessions" must appear in both _SETTINGS_DEFAULTS and _SETTINGS_BOOL_KEYS'
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


def test_show_kanban_sessions_kwarg_passthrough():
    src = _read("api/routes.py")
    assert "show_kanban_sessions=show_kanban_sessions" in src, (
        "show_kanban_sessions kwarg must be forwarded at the _dedupe_cli_sidebar_sessions_for_api call site"
    )
    assert "show_kanban=show_kanban_sessions" in src, (
        "show_kanban kwarg must be forwarded to _hide_background in _dedupe_cli_sidebar_sessions_for_api"
    )


def test_show_kanban_sessions_invalidates_session_cache_on_settings_save():
    src = _read("api/routes.py")
    invalidation_block = src.split("Settings that change which sessions appear in the sidebar", 1)[1]
    invalidation_block = invalidation_block.split("auth_enabled_after", 1)[0]
    assert '"show_kanban_sessions"' in invalidation_block, (
        "settings POST must explicitly invalidate /api/sessions cache when show_kanban_sessions changes"
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


def test_settings_show_kanban_sessions_in_html():
    src = _read("static/index.html")
    assert "settingsShowKanbanSessions" in src, (
        "settingsShowKanbanSessions checkbox must appear in static/index.html"
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
    assert "payload.show_kanban_sessions=!!(showCliCb&&showCliCb.checked&&showKanbanCb.checked)" in src, (
        "autosave wiring must gate show_kanban_sessions on settingsShowCliSessions in static/panels.js"
    )
    assert "body.show_kanban_sessions=showCliSessions&&showKanbanSessions" in src, (
        "explicit saveSettings() must gate show_kanban_sessions on showCliSessions in static/panels.js"
    )


def test_panels_load_wiring():
    src = _read("static/panels.js")
    assert "show_cron_sessions" in src, (
        "load wiring for show_cron_sessions must appear in static/panels.js"
    )
    assert "show_webhook_sessions" in src, (
        "load wiring for show_webhook_sessions must appear in static/panels.js"
    )
    assert "show_kanban_sessions" in src, (
        "load wiring for show_kanban_sessions must appear in static/panels.js"
    )


# --- #6780 gate-fix regressions: cron/webhook parity for kanban ---

def test_kanban_source_filter_overrides_default_hide():
    """An explicit kanban source_filter is a deliberate request to view kanban
    rows, so _dedupe_cli_sidebar_sessions_for_api must reveal them even though
    show_kanban_sessions is False (mirrors cron/webhook source-filter behavior)."""
    from api.routes import _dedupe_cli_sidebar_sessions_for_api

    kanban_row = {"session_id": "kb1", "source": "kanban", "message_count": 3}
    # Default (no filter): kanban is hidden.
    hidden = _dedupe_cli_sidebar_sessions_for_api(
        [dict(kanban_row)], set(), show_kanban_sessions=False, source_filter=None
    )
    assert not any(s["session_id"] == "kb1" for s in hidden), (
        "kanban row must be hidden by default with no source_filter"
    )
    # Explicit kanban filter: revealed despite show_kanban_sessions=False.
    revealed = _dedupe_cli_sidebar_sessions_for_api(
        [dict(kanban_row)], set(), show_kanban_sessions=False, source_filter="kanban"
    )
    assert any(s["session_id"] == "kb1" for s in revealed), (
        "explicit kanban source_filter must override the default hide"
    )


def test_kanban_filtered_view_capped_at_chip_limit():
    """A filtered kanban-only view (source_filter=='kanban') is bounded by a
    dedicated KANBAN_PROJECT_CHIP_LIMIT, mirroring cron/webhook. (Kanban is NOT
    added to the general exclude_sources — the original PR intentionally lets
    kanban load on the source_filter=None path so the toggle-on case works; the
    hide is applied by _hide_from_default_sidebar only when the toggle is off.)"""
    src = _read("api/models.py")
    assert "KANBAN_PROJECT_CHIP_LIMIT if source_filter == 'kanban'" in src, (
        "a bounded kanban-only project-chip limit must exist for the filtered view"
    )
    assert '("cron", "webhook") if source_filter is None' in src, (
        "kanban must NOT be excluded from the general query (would break toggle-on)"
    )


def test_kanban_source_filter_passed_to_dedupe():
    src = _read("api/routes.py")
    assert "source_filter=source_filter," in src, (
        "source_filter must be forwarded to _dedupe_cli_sidebar_sessions_for_api "
        "so an explicit kanban filter can override the hide"
    )

