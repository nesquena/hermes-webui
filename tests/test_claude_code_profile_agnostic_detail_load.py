"""Claude Code sessions must open under a NAMED (non-root) active profile.

``get_claude_code_sessions()`` scans ``~/.claude/projects`` and stamps
``profile: None`` on every row — those JSONL transcripts belong to no Hermes
profile. ``/api/sessions`` lists them regardless of the active profile, but the
``GET /api/session`` detail load ran them through
``_session_visible_to_active_profile``, which coerces ``None`` -> ``'default'``
via ``_profiles_match``. Under a named profile (e.g. ``feng-family``) that gate
404'd before ``_claim_or_synthesize_cli_session`` ever ran, so every Claude Code
row in the sidebar rendered "Session not available in web UI." when clicked.

These pin the exemption: profile-less Claude Code rows bypass the gate, while
profile-tagged foreign rows stay fully scoped (the #5419 409 contract).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import api.routes as routes
from api.models import Session


CLAUDE_SID = "claude_code_491fbe3e6ea1248d70a4177f"


def _claude_code_row():
    """A row shaped exactly like get_claude_code_sessions() emits."""
    return {
        "session_id": CLAUDE_SID,
        "title": "Claude Code transcript",
        "workspace": "/home/user/project",
        "model": "claude-code",
        "message_count": 2,
        "created_at": 1.0,
        "updated_at": 2.0,
        "last_message_at": 2.0,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "profile": None,
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
    }


def _synth_for(row):
    s = Session(
        session_id=row["session_id"],
        title=row["title"],
        workspace=row["workspace"],
        model=row["model"],
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        profile=row["profile"],
        is_cli_session=True,
        source_tag=row["source_tag"],
        raw_source=row["raw_source"],
        session_source=row["session_source"],
        source_label=row["source_label"],
        read_only=True,
    )
    return s


def _capture(monkeypatch):
    cap = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        cap["data"] = data
        cap["status"] = status
        return True

    def fake_bad(handler, msg, status=400, extra_headers=None):
        cap["error"] = msg
        cap["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    return cap


def test_claude_code_detail_load_survives_named_active_profile(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)
    synth = _synth_for(row)

    handler = MagicMock()
    parsed = urlparse(
        "/api/session?session_id=%s&messages=0&resolve_model=0" % CLAUDE_SID
    )

    with (
        patch("api.routes.get_session", side_effect=KeyError(CLAUDE_SID)),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch(
            "api.routes._claim_or_synthesize_cli_session",
            return_value=(synth, "not_claimable"),
        ),
    ):
        assert routes.handle_get(handler, parsed) is True

    assert cap.get("error") is None, (
        "profile-less Claude Code row must not be 404'd by the detail-load "
        "profile gate under a named active profile"
    )
    assert cap["status"] == 200
    sess = cap["data"]["session"]
    assert sess["session_id"] == CLAUDE_SID
    assert sess["read_only"] is True
    assert sess["is_cli_session"] is True
    assert sess["source_tag"] == "claude_code"
    assert len(sess["messages"]) == 2


def test_profile_tagged_foreign_session_still_scoped(monkeypatch):
    """Negative control: a row that DOES carry a profile keeps the #5419 409."""
    row = dict(_claude_code_row())
    row.update(
        session_id="20260101_000000_abc123",
        profile="other-profile",
        source_tag="telegram",
        raw_source="telegram",
        session_source="messaging",
        source_label="Telegram",
    )
    cap = _capture(monkeypatch)

    handler = MagicMock()
    parsed = urlparse(
        "/api/session?session_id=%s&messages=0&resolve_model=0" % row["session_id"]
    )

    with (
        patch("api.routes.get_session", side_effect=KeyError(row["session_id"])),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
    ):
        assert routes.handle_get(handler, parsed) is True

    assert cap["status"] == 409
    assert cap["data"]["code"] == "session_profile_mismatch"


def test_profile_agnostic_predicate_is_narrow():
    assert routes._is_profile_agnostic_foreign_session(_claude_code_row()) is True
    # A Claude Code row that somehow carries a profile stays scoped.
    tagged = dict(_claude_code_row(), profile="feng-family")
    assert routes._is_profile_agnostic_foreign_session(tagged) is False
    # A profile-less row from any other source stays scoped.
    other = dict(_claude_code_row(), source_tag="cli", raw_source="cli")
    assert routes._is_profile_agnostic_foreign_session(other) is False
    # A Claude Code row that is not read-only stays scoped.
    writable = dict(_claude_code_row(), read_only=False)
    assert routes._is_profile_agnostic_foreign_session(writable) is False
    # A Claude Code row that is not from external-agent provenance stays scoped.
    non_external = dict(_claude_code_row(), session_source="webui")
    assert routes._is_profile_agnostic_foreign_session(non_external) is False
    # Missing / empty metadata is never exempt.
    assert routes._is_profile_agnostic_foreign_session({}) is False
    assert routes._is_profile_agnostic_foreign_session(None) is False


def test_isolated_profile_mode_blocks_claude_code_detail_load(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)

    handler = MagicMock()
    parsed = urlparse(
        "/api/session?session_id=%s&messages=0&resolve_model=0" % CLAUDE_SID
    )

    with (
        patch("api.routes.get_session", side_effect=KeyError(CLAUDE_SID)),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes.handle_get(handler, parsed) is True

    # Under isolated profile mode, detail load must fail with 404
    assert cap["status"] == 404
    assert cap.get("error") == "Session not found"


def test_isolated_profile_mode_blocks_claude_code_sharing(monkeypatch):
    row = _claude_code_row()
    handler = MagicMock()

    with (
        patch("api.routes.get_session", side_effect=KeyError(CLAUDE_SID)),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        import pytest

        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(CLAUDE_SID, handler)


def test_isolated_profile_mode_blocks_stored_claude_code_sharing(monkeypatch):
    row = _claude_code_row()
    handler = MagicMock()
    mock_stored = MagicMock()
    mock_stored.profile = None
    mock_stored.read_only = True
    mock_stored.is_cli_session = True
    mock_stored.session_source = "external_agent"
    mock_stored.source_tag = "claude_code"
    mock_stored.compact.return_value = row

    mock_ensure = MagicMock()

    with (
        patch("api.routes.get_session", return_value=mock_stored),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
        patch("api.routes._ensure_full_session_before_mutation", mock_ensure),
    ):
        import pytest

        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(CLAUDE_SID, handler)

    assert mock_ensure.call_count == 0


def test_isolated_profile_mode_blocks_claude_code_import(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)
    body = {"session_id": CLAUDE_SID, "profile": "feng-family"}

    handler = MagicMock()
    mock_get_msgs = MagicMock(
        return_value=[{"role": "user", "content": "secret isolated content"}]
    )
    mock_load = MagicMock(return_value=None)

    with (
        patch("api.routes.Session.load", mock_load),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes.get_cli_session_messages", mock_get_msgs),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes._handle_session_import_cli(handler, body) is True

    # Under isolated profile mode, import must fail with 404
    assert cap["status"] == 404
    assert cap.get("error") == "Session not found in CLI store"
    # Ensure that neither sidecar load nor get_cli_session_messages were ever called
    assert mock_load.call_count == 0
    assert mock_get_msgs.call_count == 0


def test_isolated_profile_mode_blocks_stored_claude_code_detail_load(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)

    mock_stored = MagicMock()
    mock_stored.profile = None
    mock_stored.read_only = True
    mock_stored.is_cli_session = True
    mock_stored.session_source = "external_agent"
    mock_stored.source_tag = "claude_code"
    mock_stored.compact.return_value = row

    handler = MagicMock()
    parsed = urlparse(
        "/api/session?session_id=%s&messages=1&resolve_model=0" % CLAUDE_SID
    )

    with (
        patch("api.routes.get_session", return_value=mock_stored),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes.handle_get(handler, parsed) is True

    assert cap["status"] == 404
    assert cap.get("error") == "Session not found"


def test_isolated_profile_mode_filters_gateway_sse_snapshot():
    claude_row = _claude_code_row()
    active_row = {"session_id": "active-1", "profile": "feng-family"}
    other_row = {"session_id": "other-1", "profile": "other-profile"}
    rows = [claude_row, active_row, other_row]

    # Non-isolated mode: active row and agnostic Claude row are visible, other-profile row is excluded
    non_isolated_scoped = routes._scope_rows_to_active_profile(
        rows, "feng-family", is_isolated=False
    )
    assert {r["session_id"] for r in non_isolated_scoped} == {
        CLAUDE_SID,
        "active-1",
    }

    # Isolated mode: only the active row is visible; agnostic Claude row and other-profile row are excluded
    isolated_scoped = routes._scope_rows_to_active_profile(
        rows, "feng-family", is_isolated=True
    )
    assert {r["session_id"] for r in isolated_scoped} == {"active-1"}

    # Isolated mode under default profile: agnostic Claude row is still excluded
    default_isolated_scoped = routes._scope_rows_to_active_profile(
        rows, "default", is_isolated=True
    )
    assert {r["session_id"] for r in default_isolated_scoped} == set()


def test_isolated_profile_mode_gateway_sse_stream_handler(monkeypatch):
    claude_row = _claude_code_row()
    active_row = {"session_id": "active-1", "profile": "feng-family"}
    other_row = {"session_id": "other-1", "profile": "other-profile"}
    initial_rows = [claude_row, active_row, other_row]

    sent_events = []

    def mock_sse(handler, event_type, data):
        sent_events.append((event_type, data))
        if len(sent_events) >= 2:
            raise ConnectionResetError("test stop after loop event")

    handler = MagicMock()
    mock_queue = MagicMock()
    shared_event = {"type": "sessions_changed", "sessions": [claude_row, active_row, other_row]}
    mock_queue.get.return_value = shared_event
    mock_watcher = MagicMock()
    mock_watcher.is_alive.return_value = True
    mock_watcher.subscribe.return_value = mock_queue

    with (
        patch("api.routes.load_settings", return_value={"show_cli_sessions": True}),
        patch("api.gateway_watcher.get_watcher", return_value=mock_watcher),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.models.get_cli_sessions", return_value=initial_rows),
        patch("api.routes._sse", side_effect=mock_sse),
        patch("api.routes.end_sse_headers"),
        patch("api.routes._sse_set_write_deadline"),
    ):
        routes._handle_gateway_sse_stream(handler, urlparse("/api/sessions/gateway/stream"))

    assert len(sent_events) == 2
    # Snapshot:
    assert sent_events[0][0] == "sessions_changed"
    assert {r["session_id"] for r in sent_events[0][1]["sessions"]} == {"active-1"}
    # Stream event:
    assert sent_events[1][0] == "sessions_changed"
    assert {r["session_id"] for r in sent_events[1][1]["sessions"]} == {"active-1"}
    # Original shared event dictionary was not mutated in place:
    assert len(shared_event["sessions"]) == 3


def test_stored_claude_code_detail_load_survives_named_profile(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)

    mock_stored = MagicMock()
    mock_stored.session_id = CLAUDE_SID
    mock_stored.profile = None
    mock_stored.read_only = True
    mock_stored.is_cli_session = True
    mock_stored.session_source = "external_agent"
    mock_stored.source_tag = "claude_code"
    mock_stored.messages = []
    mock_stored.active_stream_id = None
    mock_stored.compact.return_value = row

    handler = MagicMock()
    parsed = urlparse(
        "/api/session?session_id=%s&messages=0&resolve_model=0" % CLAUDE_SID
    )

    with (
        patch("api.routes.get_session", return_value=mock_stored),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
    ):
        assert routes.handle_get(handler, parsed) is True

    assert cap.get("error") is None
    assert cap.get("status") == 200


def test_stored_claude_code_sharing_survives_named_profile(monkeypatch):
    row = _claude_code_row()
    handler = MagicMock()
    mock_stored = MagicMock()
    mock_stored.session_id = CLAUDE_SID
    mock_stored.profile = None
    mock_stored.read_only = True
    mock_stored.is_cli_session = True
    mock_stored.session_source = "external_agent"
    mock_stored.source_tag = "claude_code"
    mock_stored.messages = [{"role": "user", "content": "hello"}]
    mock_stored.compact.return_value = row

    with (
        patch("api.routes.get_session", return_value=mock_stored),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._ensure_full_session_before_mutation", side_effect=lambda sid, s: s),
    ):
        snap, stored, meta = routes._resolve_share_session_pair(CLAUDE_SID, handler)
        assert snap is not None
        assert stored is mock_stored


def test_synthesized_claude_code_sharing_survives_named_profile(monkeypatch):
    row = _claude_code_row()
    handler = MagicMock()
    synth = _synth_for(row)

    with (
        patch("api.routes.get_session", side_effect=KeyError(CLAUDE_SID)),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._claim_or_synthesize_cli_session", return_value=(synth, "not_claimable")),
    ):
        snap, stored, meta = routes._resolve_share_session_pair(CLAUDE_SID, handler)
        assert snap is synth
        assert stored is None


def test_reimport_existing_claude_code_session_survives_named_profile(monkeypatch):
    row = _claude_code_row()
    cap = _capture(monkeypatch)
    body = {"session_id": CLAUDE_SID, "profile": "feng-family"}

    existing = MagicMock()
    existing.session_id = CLAUDE_SID
    existing.profile = None
    existing.read_only = True
    existing.is_cli_session = True
    existing.session_source = "external_agent"
    existing.source_tag = "claude_code"
    existing.messages = [{"role": "user", "content": "original"}]
    existing.compact.return_value = row

    fresh_messages = [
        {"role": "user", "content": "original"},
        {"role": "assistant", "content": "reply"},
    ]

    handler = MagicMock()
    with (
        patch("api.routes.Session.load", return_value=existing),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes.get_cli_session_messages", return_value=fresh_messages),
    ):
        assert routes._handle_session_import_cli(handler, body) is True

    assert cap.get("error") is None
    assert cap.get("status") == 200
    assert existing.messages == fresh_messages
