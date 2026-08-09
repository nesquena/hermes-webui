"""Last-writer-wins title reconcile for #3986.

The WebUI sidebar shows a session's sidecar title unless state.db overrides
it. #3994 added adoption of the state.db title only when the sidecar title is
still a generic/placeholder ("Hermes WebUI" / "Hermes WebUI #N"). This module
extends that reconcile with a last-writer-wins rule: when the agent side
(state.db `last_activity_at`) wrote after the WebUI side (sidecar
`updated_at`), the state.db title wins even for non-placeholder titles —
covering TUI/CLI/Desktop renames of sessions that already have a real WebUI
title. The WebUI-wrote-last direction (a WebUI rename with
`sync_to_insights` off, where state.db still holds the old title) keeps the
sidecar title, preserving #3994's no-clobber guarantee.
"""

import pytest


def _apply(session, metadata_entry):
    from api.models import _apply_sidebar_state_db_override_metadata
    sessions = [dict(session)]
    _apply_sidebar_state_db_override_metadata(sessions, {session["session_id"]: dict(metadata_entry)})
    return sessions[0]


@pytest.fixture
def base_session():
    return {
        "session_id": "sid-3986",
        "title": "Old sidecar title",
        "updated_at": 100,
        "last_message_at": 100,
        "message_count": 10,
    }


def test_state_db_title_wins_when_agent_wrote_last(base_session):
    # TUI/CLI rename: state.db last_activity_at is newer than the sidecar
    # updated_at -> the (non-placeholder) sidecar title must be replaced.
    session = _apply(
        base_session,
        {
            "_state_db_title": "New TUI title",
            "_state_db_last_activity_at": 200,
        },
    )
    assert session["display_title"] == "New TUI title"
    assert session["_state_db_title"] == "New TUI title"


def test_sidecar_title_kept_when_webui_wrote_last(base_session):
    # WebUI rename with sync_to_insights off: sidecar updated_at is newer than
    # state.db last_activity_at -> state.db's old title must NOT clobber the
    # sidecar title the user set in the WebUI.
    session = _apply(
        base_session,
        {
            "_state_db_title": "Old state title",
            "_state_db_last_activity_at": 50,
        },
    )
    assert session.get("display_title") is None
    assert session.get("_state_db_title") is None
    assert session["title"] == "Old sidecar title"


def test_generic_placeholder_title_still_adopts_state_db_title(base_session):
    # #3994 regression guard: generic placeholder titles keep the
    # unconditional state.db adoption regardless of timestamps.
    session = dict(base_session)
    session["title"] = "Hermes WebUI #42"
    session = _apply(
        session,
        {
            "_state_db_title": "Real title",
            "_state_db_last_activity_at": 50,
        },
    )
    assert session["display_title"] == "Real title"


def test_null_state_db_title_keeps_sidecar_title(base_session):
    # state.db title NULL (legacy sessions): no override at all.
    session = _apply(base_session, {"_state_db_last_activity_at": 500})
    assert session.get("display_title") is None
    assert session["title"] == "Old sidecar title"


def test_equal_timestamps_keep_sidecar_title(base_session):
    # No strictly-newer writer: prefer the sidecar (no clobber on ties).
    session = _apply(
        base_session,
        {
            "_state_db_title": "Tied title",
            "_state_db_last_activity_at": 100,
        },
    )
    assert session.get("display_title") is None
    assert session["title"] == "Old sidecar title"


def test_overrides_query_carries_last_activity_at():
    # Source-shape guard: the sidebar overrides query must fetch
    # last_activity_at or the last-writer comparison silently degrades.
    import inspect
    from api import models
    src = inspect.getsource(models._read_state_db_sidebar_overrides)
    assert "last_activity_expr" in src
    assert "last_activity_at" in src
