"""Regression coverage for Matrix sidebar visibility and filter overrides."""

from api.models import _hide_from_default_sidebar


def _matrix_row(session_id="matrix-room-1"):
    return {
        "session_id": session_id,
        "source": "matrix",
        "raw_source": "matrix",
        "session_source": "messaging",
        "source_tag": "matrix",
        "message_count": 3,
        "project_id": None,
        "profile": "default",
        "updated_at": 3,
    }


def test_matrix_is_hidden_by_default_and_visible_when_enabled():
    row = _matrix_row()

    assert _hide_from_default_sidebar(row) is True
    assert _hide_from_default_sidebar(row, show_matrix=True) is False


def test_non_matrix_messaging_is_not_hidden_by_matrix_setting():
    row = _matrix_row("telegram-1")
    row["source"] = row["raw_source"] = row["source_tag"] = "telegram"

    assert _hide_from_default_sidebar(row) is False
    assert _hide_from_default_sidebar(row, show_matrix=False) is False


def test_explicit_matrix_source_filter_reveals_rows_when_setting_is_off():
    from api.routes import _dedupe_cli_sidebar_sessions_for_api

    rows = _dedupe_cli_sidebar_sessions_for_api(
        [_matrix_row()],
        set(),
        show_matrix_sessions=False,
        source_filter="matrix",
    )

    assert [row["session_id"] for row in rows] == ["matrix-room-1"]


def test_explicit_sidebar_origin_filter_reveals_hidden_rows_when_setting_is_off(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    telegram_row = _matrix_row("telegram-1")
    telegram_row.update({
        "source": "telegram",
        "raw_source": "telegram",
        "source_tag": "telegram",
    })
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [telegram_row])

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=False,
        sidebar_source="telegram",
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["telegram-1"]


def test_matrix_visibility_is_part_of_session_cache_key():
    from api.routes import _session_list_cache_key

    hidden_key = _session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=False,
    )
    visible_key = _session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=True,
    )

    assert hidden_key != visible_key


def test_matrix_visibility_is_reported_by_session_list_builder(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda **_kwargs: [_matrix_row()])

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_matrix_sessions=False,
    )

    assert payload["settings"]["show_matrix_sessions"] is False
    assert payload["sessions"] == []
