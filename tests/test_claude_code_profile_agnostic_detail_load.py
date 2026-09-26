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

import os
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

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


@pytest.mark.parametrize("messages", ["0", "1"])
def test_isolated_profile_mode_stored_foreign_profile_returns_409(
    monkeypatch, messages
):
    sid = "stored_other_profile"
    stored = Session(
        session_id=sid,
        title="Other profile session",
        workspace="/tmp",
        model="test-model",
        messages=[],
        created_at=1.0,
        updated_at=2.0,
        profile="other-profile",
    )
    cap = _capture(monkeypatch)

    with (
        patch("api.routes.get_session", return_value=stored),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes.handle_get(
            MagicMock(),
            urlparse(
                f"/api/session?session_id={sid}&messages={messages}&resolve_model=0"
            ),
        ) is True

    assert cap["status"] == 409
    assert cap["data"] == {
        "error": "Session belongs to a different profile",
        "code": "session_profile_mismatch",
        "session_id": sid,
        "profile": "other-profile",
    }


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


def test_isolated_profile_mode_blocks_existing_claude_code_import(monkeypatch):
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
    existing.raw_source = "claude_code"
    existing.messages = [{"role": "user", "content": "secret isolated content"}]
    existing.compact.return_value = row

    mock_get_msgs = MagicMock()
    handler = MagicMock()
    with (
        patch("api.routes.Session.load", return_value=existing),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=None),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
        patch("api.routes.get_cli_session_messages", mock_get_msgs),
    ):
        assert routes._handle_session_import_cli(handler, body) is True

    assert cap["status"] == 404
    assert cap.get("error") == "Session not found in CLI store"
    assert mock_get_msgs.call_count == 0


def test_import_cli_existing_foreign_profile_returns_409_mismatch(monkeypatch):
    cap = _capture(monkeypatch)
    body = {"session_id": "other_sid"}

    existing = MagicMock()
    existing.session_id = "other_sid"
    existing.profile = "work-profile"
    existing.read_only = False
    existing.is_cli_session = True
    existing.session_source = "hermes"
    existing.source_tag = "cli"
    existing.messages = [{"role": "user", "content": "secret"}]

    mock_get_msgs = MagicMock()
    handler = MagicMock()
    with (
        patch("api.routes.Session.load", return_value=existing),
        patch("api.routes._get_active_profile_name", return_value="default"),
        patch("api.routes._lookup_cli_session_metadata", return_value=None),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
        patch("api.routes.get_cli_session_messages", mock_get_msgs),
    ):
        assert routes._handle_session_import_cli(handler, body) is True

    assert cap["status"] == 409
    assert cap.get("data", {}).get("code") == "session_profile_mismatch"
    assert cap.get("data", {}).get("profile") == "work-profile"
    assert cap.get("data", {}).get("session_id") == "other_sid"
    assert mock_get_msgs.call_count == 0


# ── Cache-stale isolation (fix spec #1) ──────────────────────────────────────
#
# The isolation gates used to decide "profile-agnostic" from the CLI metadata
# row, and a MISSING row reads as "not agnostic". The message readers do not
# share that blind spot: get_cli_session_messages() routes on the
# ``claude_code_`` id prefix and scans ~/.claude/projects directly. So with a
# transcript already on disk but not yet in the metadata cache, an isolated
# deployment could import it (as a writable sidecar), open it, and share it.
# These pin the id-first gate: 404 with no sidecar load, no metadata-driven
# synthesis and no transcript read.


def _stale_cache_disk_messages():
    """What the JSONL scanner would return for a transcript not yet cached."""
    return [
        {"role": "user", "content": "isolated deployment must never see this"},
        {"role": "assistant", "content": "leaked reply"},
    ]


def test_isolated_import_rejects_claude_code_when_metadata_cache_is_stale(monkeypatch):
    cap = _capture(monkeypatch)
    body = {"session_id": CLAUDE_SID, "profile": "ops"}

    # Cold/stale cache: no row for a transcript whose JSONL already exists.
    mock_lookup = MagicMock(return_value=None)
    mock_resolve = MagicMock(return_value={})
    mock_load = MagicMock(return_value=None)
    mock_get_msgs = MagicMock(return_value=_stale_cache_disk_messages())
    mock_import = MagicMock()

    with (
        patch("api.routes.Session.load", mock_load),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", mock_lookup),
        patch("api.routes._resolve_cli_import_metadata", mock_resolve),
        patch("api.routes.get_cli_session_messages", mock_get_msgs),
        patch("api.routes.import_cli_session", mock_import),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes._handle_session_import_cli(handler := MagicMock(), body) is True
        assert handler is not None

    assert cap["status"] == 404
    assert cap.get("error") == "Session not found in CLI store"
    # No sidecar read/mutation and no transcript read on the rejected path.
    assert mock_load.call_count == 0
    assert mock_import.call_count == 0
    assert mock_get_msgs.call_count == 0
    assert mock_lookup.call_count == 0
    assert mock_resolve.call_count == 0


def test_isolated_detail_load_rejects_claude_code_when_metadata_cache_is_stale(monkeypatch):
    cap = _capture(monkeypatch)

    mock_get_session = MagicMock(side_effect=KeyError(CLAUDE_SID))
    mock_lookup = MagicMock(return_value={})
    mock_synth = MagicMock(return_value=(_synth_for(_claude_code_row()), "not_claimable"))
    mock_get_msgs = MagicMock(return_value=_stale_cache_disk_messages())

    parsed = urlparse(
        "/api/session?session_id=%s&messages=1&resolve_model=0" % CLAUDE_SID
    )
    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", mock_lookup),
        patch("api.routes._claim_or_synthesize_cli_session", mock_synth),
        patch("api.routes.get_cli_session_messages", mock_get_msgs),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        assert routes.handle_get(MagicMock(), parsed) is True

    assert cap["status"] == 404
    assert cap.get("error") == "Session not found"
    # The gate runs before the sidecar load, the metadata lookup and any
    # synthesis, so nothing on disk is touched.
    assert mock_get_session.call_count == 0
    assert mock_lookup.call_count == 0
    assert mock_synth.call_count == 0
    assert mock_get_msgs.call_count == 0


def test_isolated_share_rejects_claude_code_when_metadata_cache_is_stale(monkeypatch):
    mock_get_session = MagicMock(side_effect=KeyError(CLAUDE_SID))
    mock_lookup = MagicMock(return_value={})
    mock_synth = MagicMock(return_value=(_synth_for(_claude_code_row()), "not_claimable"))

    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", mock_lookup),
        patch("api.routes._claim_or_synthesize_cli_session", mock_synth),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(CLAUDE_SID, MagicMock())

    assert mock_get_session.call_count == 0
    assert mock_lookup.call_count == 0
    assert mock_synth.call_count == 0


def test_isolated_sidebar_drops_claude_code_row_that_carries_a_profile():
    """A stale row with a profile misses the metadata shape — the id still wins."""
    stale_row = dict(_claude_code_row(), profile="ops", read_only=False)
    assert routes._is_profile_agnostic_foreign_session(stale_row) is False
    scoped = routes._scope_rows_to_active_profile(
        [stale_row, {"session_id": "active-1", "profile": "ops"}],
        "ops",
        is_isolated=True,
    )
    assert {r["session_id"] for r in scoped} == {"active-1"}


def test_profile_agnostic_session_id_predicate():
    assert routes._is_profile_agnostic_session_id(CLAUDE_SID) is True
    assert routes._is_profile_agnostic_session_id("  " + CLAUDE_SID + "  ") is True
    assert routes._is_profile_agnostic_session_id("claude_code_") is True
    assert routes._is_profile_agnostic_session_id("20260101_000000_abc123") is False
    assert routes._is_profile_agnostic_session_id("") is False
    assert routes._is_profile_agnostic_session_id(None) is False
    # A non-agnostic store must not be laundered in by a lookalike prefix.
    assert routes._is_profile_agnostic_session_id("codex_session_123") is False


def test_isolated_rejection_with_real_jsonl_file_and_stale_cache(monkeypatch, tmp_path):
    """End-to-end regression: real JSONL exists on disk but CLI metadata cache is stale."""
    import json
    import api.models as models

    projects_dir = tmp_path / "claude" / "projects"
    session_file = projects_dir / "proj" / "session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"summary": "Secret Real Session"},
        {"timestamp": "2026-04-18T12:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "secret unread text"}]}},
    ]
    session_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", str(projects_dir))

    sid = models._claude_code_session_id(session_file)
    assert sid.startswith("claude_code_")

    # Real scanner verifies the transcript exists on disk and is readable
    disk_msgs = models.get_claude_code_session_messages(sid, projects_dir=projects_dir)
    assert len(disk_msgs) == 1
    assert disk_msgs[0]["content"] == "secret unread text"

    cap = _capture(monkeypatch)
    with (
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value=None),
        patch("api.routes._is_isolated_profile_mode", return_value=True),
    ):
        # 1. Import returns 404 and does not mutate or read messages
        assert routes._handle_session_import_cli(MagicMock(), {"session_id": sid, "profile": "ops"}) is True
        assert cap["status"] == 404
        assert cap.get("error") == "Session not found in CLI store"

        # 2. Detail load returns 404
        parsed = urlparse(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
        assert routes.handle_get(MagicMock(), parsed) is True
        assert cap["status"] == 404
        assert cap.get("error") == "Session not found"

        # 3. Share resolution raises KeyError
        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(sid, MagicMock())


# ── Stored foreign owner outranks a profile-less metadata row (fix spec #2) ──
#
# The profile-less exemption ran BEFORE the stored-profile check, so a
# persisted sidecar owned by `other` matched against a Claude metadata row with
# no profile was treated as belonging to no profile at all: detail load
# returned 200 with the transcript instead of the #5419 409, and sharing was
# allowed.


FOREIGN_OWNED_SECRET = "message owned by the other profile"


def _foreign_owned_claude_sidecar():
    return Session(
        session_id=CLAUDE_SID,
        title="Imported Claude Code transcript",
        workspace="/home/user/project",
        model="claude-code",
        messages=[{"role": "user", "content": FOREIGN_OWNED_SECRET}],
        created_at=1.0,
        updated_at=2.0,
        profile="other",
        is_cli_session=True,
        source_tag="claude_code",
        raw_source="claude_code",
        session_source="external_agent",
        source_label="Claude Code",
        read_only=True,
    )


@pytest.mark.parametrize("messages", ["0", "1"])
def test_stored_foreign_owner_beats_profile_less_claude_metadata_on_detail_load(
    monkeypatch, messages
):
    cap = _capture(monkeypatch)
    stored = _foreign_owned_claude_sidecar()
    # The Claude metadata row carries no profile — the old exemption fired here.
    agnostic_meta = _claude_code_row()

    mock_get_session = MagicMock(return_value=stored)
    parsed = urlparse(
        "/api/session?session_id=%s&messages=%s&resolve_model=0" % (CLAUDE_SID, messages)
    )
    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value=agnostic_meta),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
    ):
        assert routes.handle_get(MagicMock(), parsed) is True

    assert cap["status"] == 409
    assert cap["data"] == {
        "error": "Session belongs to a different profile",
        "code": "session_profile_mismatch",
        "session_id": CLAUDE_SID,
        "profile": "other",
    }
    # Never hydrated: the gate rejects on metadata alone.
    assert mock_get_session.call_count >= 1
    assert all(
        call.kwargs.get("metadata_only") is True
        for call in mock_get_session.call_args_list
    )
    assert FOREIGN_OWNED_SECRET not in repr(cap["data"])


def test_stored_foreign_owner_beats_profile_less_claude_metadata_on_share(monkeypatch):
    stored = _foreign_owned_claude_sidecar()
    mock_get_session = MagicMock(return_value=stored)
    mock_snapshot = MagicMock()

    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value=_claude_code_row()),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._share_snapshot_messages_for_session", mock_snapshot),
    ):
        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(CLAUDE_SID, MagicMock())

    # Denied before the share snapshot (and its message load) is built.
    assert mock_snapshot.call_count == 0


def test_stored_active_owner_with_profile_less_claude_metadata_still_loads(monkeypatch):
    """Negative control: the owner check only denies a FOREIGN owner."""
    cap = _capture(monkeypatch)
    stored = _foreign_owned_claude_sidecar()
    stored.profile = "ops"

    parsed = urlparse(
        "/api/session?session_id=%s&messages=0&resolve_model=0" % CLAUDE_SID
    )
    with (
        patch("api.routes.get_session", return_value=stored),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value=_claude_code_row()),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
    ):
        assert routes.handle_get(MagicMock(), parsed) is True

    assert cap.get("error") is None
    assert cap["status"] == 200


def test_stored_foreign_owner_beats_active_profile_cli_metadata_on_share(monkeypatch):
    """Stored foreign sidecar profile='other' outranks conflicting CLI metadata profile='ops'."""
    stored = _foreign_owned_claude_sidecar()
    stored.profile = "other"
    conflicting_meta = dict(_claude_code_row(), profile="ops")
    mock_get_session = MagicMock(return_value=stored)
    mock_snapshot = MagicMock()

    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value=conflicting_meta),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._share_snapshot_messages_for_session", mock_snapshot),
    ):
        with pytest.raises(KeyError):
            routes._resolve_share_session_pair(CLAUDE_SID, MagicMock())

    assert mock_snapshot.call_count == 0


# ── Live gateway rows must survive the SSE profile filter (fix spec #1) ──────
#
# The SSE handler scopes every pushed event with
# `_scope_rows_to_active_profile`, but the rows the gateway watcher projects
# out of state.db carry no `profile` key at all. `_profiles_match` coerces a
# missing profile to 'default', so under a named profile the filter dropped
# every gateway row: the browser got the session in the initial snapshot and
# an empty list on the next live update, skipped the active transcript refresh,
# and gateway sessions stopped updating live.


def _watcher_projection_row(session_id="tg-1"):
    """A row shaped exactly like gateway_watcher._get_agent_sessions_from_db emits."""
    return {
        "session_id": session_id,
        "title": "Telegram chat",
        "model": None,
        "message_count": 3,
        "created_at": 1.0,
        "updated_at": 2.0,
        "source": "telegram",
        "raw_source": "telegram",
        "session_source": "gateway",
        "source_label": "Telegram",
    }


def _run_gateway_sse_stream(
    *, initial_rows, live_rows, active_profile, watcher_profile, is_isolated=False
):
    """Drive _handle_gateway_sse_stream for one snapshot + one live event."""
    sent_events = []

    def mock_sse(handler, event_type, data):
        sent_events.append((event_type, data))
        if len(sent_events) >= 2:
            raise ConnectionResetError("test stop after loop event")

    shared_event = {"type": "sessions_changed", "sessions": live_rows}
    mock_queue = MagicMock()
    mock_queue.get.return_value = shared_event
    mock_watcher = MagicMock()
    mock_watcher.is_alive.return_value = True
    mock_watcher.subscribe.return_value = mock_queue
    mock_watcher.profile_name = watcher_profile

    with (
        patch("api.routes.load_settings", return_value={"show_cli_sessions": True}),
        patch("api.gateway_watcher.get_watcher", return_value=mock_watcher),
        patch("api.routes._is_isolated_profile_mode", return_value=is_isolated),
        patch("api.routes._get_active_profile_name", return_value=active_profile),
        patch("api.models.get_cli_sessions", return_value=initial_rows),
        patch("api.routes._sse", side_effect=mock_sse),
        patch("api.routes.end_sse_headers"),
        patch("api.routes._sse_set_write_deadline"),
    ):
        routes._handle_gateway_sse_stream(
            MagicMock(), urlparse("/api/sessions/gateway/stream")
        )

    assert len(sent_events) == 2
    return sent_events, shared_event


def test_gateway_sse_live_event_keeps_unstamped_watcher_rows_under_named_profile():
    row = _watcher_projection_row()
    assert "profile" not in row, "watcher rows genuinely carry no profile key"

    sent_events, shared_event = _run_gateway_sse_stream(
        initial_rows=[row],
        live_rows=[row],
        active_profile="feng-family",
        watcher_profile="feng-family",
    )

    snapshot, live = sent_events
    assert {r["session_id"] for r in snapshot[1]["sessions"]} == {"tg-1"}
    assert {r["session_id"] for r in live[1]["sessions"]} == {"tg-1"}, (
        "an unstamped watcher row must be attributed to the watcher's own "
        "profile, not dropped as a 'default'-profile row"
    )
    # The shared event dict (and its rows) is handed to every subscriber —
    # scoping must not mutate it in place.
    assert shared_event["sessions"] == [row]
    assert "profile" not in row


def test_gateway_sse_live_event_survives_watcher_without_profile_name():
    """A watcher constructed without a profile name falls back to the active one."""
    row = _watcher_projection_row("tg-2")

    sent_events, _shared = _run_gateway_sse_stream(
        initial_rows=[row],
        live_rows=[row],
        active_profile="feng-family",
        watcher_profile="",
    )

    assert {r["session_id"] for r in sent_events[1][1]["sessions"]} == {"tg-2"}


def test_gateway_sse_attribution_never_overwrites_explicit_profiles():
    """Rows that already name a profile (including None) keep their own scope."""
    claude_row = _claude_code_row()  # explicit profile: None -> stays agnostic
    other_row = {"session_id": "other-1", "profile": "other-profile"}
    watcher_row = _watcher_projection_row()
    rows = [claude_row, other_row, watcher_row]

    sent_events, _shared = _run_gateway_sse_stream(
        initial_rows=rows,
        live_rows=rows,
        active_profile="feng-family",
        watcher_profile="feng-family",
    )

    for _event_type, data in sent_events:
        assert {r["session_id"] for r in data["sessions"]} == {CLAUDE_SID, "tg-1"}
    assert claude_row["profile"] is None


def test_gateway_watcher_stamps_owning_profile_on_projected_rows(monkeypatch):
    """The watcher itself stamps the profile whose state.db it read."""
    from api import gateway_watcher as gw

    watcher = gw.GatewayWatcher(profile_name="feng-family")
    watcher.subscribe()
    monkeypatch.setattr(
        gw, "_get_agent_sessions_from_db", lambda _path: [_watcher_projection_row()]
    )
    monkeypatch.setattr(gw, "_cheap_change_fingerprint", lambda _path: "fp-1")
    monkeypatch.setattr(type(watcher._state_db_path), "exists", lambda _self: True)

    assert watcher._poll_once(now=1.0) is True
    assert [r["profile"] for r in watcher._last_sessions] == ["feng-family"]

    # An explicit profile (including the agnostic None) is never overwritten.
    stamped = watcher._attribute_owning_profile(
        [dict(_watcher_projection_row(), profile=None), {"session_id": "x", "profile": "other"}]
    )
    assert [r["profile"] for r in stamped] == [None, "other"]


# ── Detail and share must authorize the load they actually use (fix spec #3) ─
#
# Both handlers authorized on a metadata-only load and then hydrated the
# session with a second get_session(metadata_only=False) whose owner was never
# re-checked. An empty placeholder can be re-tagged to a profile during chat
# start, so the second load could return another profile's transcript.


RETAGGED_SECRET = "message that belongs to the other profile"


def _retagging_get_session(first_profile, second_profile, secret=RETAGGED_SECRET):
    """get_session double whose sidecar is re-tagged between the two loads."""

    def _make(profile, messages):
        return Session(
            session_id="placeholder-sid",
            title="Placeholder",
            workspace="/tmp",
            model="test-model",
            messages=messages,
            created_at=1.0,
            updated_at=2.0,
            profile=profile,
        )

    def _side_effect(sid, metadata_only=False, **_kwargs):
        if metadata_only:
            return _make(first_profile, [])
        return _make(second_profile, [{"role": "user", "content": secret}])

    return MagicMock(side_effect=_side_effect)


def test_detail_load_rechecks_profile_after_the_full_load(monkeypatch):
    cap = _capture(monkeypatch)
    mock_get_session = _retagging_get_session("ops", "other")

    parsed = urlparse(
        "/api/session?session_id=placeholder-sid&messages=1&resolve_model=0"
    )
    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value={}),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
    ):
        assert routes.handle_get(MagicMock(), parsed) is True

    assert cap["status"] == 409
    assert cap["data"] == {
        "error": "Session belongs to a different profile",
        "code": "session_profile_mismatch",
        "session_id": "placeholder-sid",
        "profile": "other",
    }
    # Both loads ran; the messages from the second one never reached the client.
    assert mock_get_session.call_count == 2
    assert RETAGGED_SECRET not in repr(cap["data"])


def test_detail_load_recheck_allows_an_unchanged_owner(monkeypatch):
    """Negative control: the re-check only denies when ownership actually moved."""
    cap = _capture(monkeypatch)
    mock_get_session = _retagging_get_session("ops", "ops")

    parsed = urlparse(
        "/api/session?session_id=placeholder-sid&messages=1&resolve_model=0"
    )
    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value={}),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
    ):
        assert routes.handle_get(MagicMock(), parsed) is True

    assert cap.get("error") is None
    assert cap["status"] == 200
    assert RETAGGED_SECRET in repr(cap["data"])


def test_share_rechecks_profile_after_the_full_load(monkeypatch):
    mock_get_session = _retagging_get_session("ops", "other")
    mock_snapshot = MagicMock()

    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value={}),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._share_snapshot_messages_for_session", mock_snapshot),
    ):
        with pytest.raises(KeyError):
            routes._resolve_share_session_pair("placeholder-sid", MagicMock())

    assert mock_get_session.call_count == 2
    # Denied before the share snapshot (and its message load) is built.
    assert mock_snapshot.call_count == 0


def test_share_recheck_allows_an_unchanged_owner(monkeypatch):
    """Negative control: an owner that did not move still shares."""
    mock_get_session = _retagging_get_session("ops", "ops")
    mock_snapshot = MagicMock(return_value=[{"role": "user", "content": RETAGGED_SECRET}])

    with (
        patch("api.routes.get_session", mock_get_session),
        patch("api.routes._get_active_profile_name", return_value="ops"),
        patch("api.routes._lookup_cli_session_metadata", return_value={}),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes._share_snapshot_messages_for_session", mock_snapshot),
    ):
        snapshot_session, stored_session, _cli_meta = routes._resolve_share_session_pair(
            "placeholder-sid", MagicMock()
        )

    assert mock_snapshot.call_count == 1
    assert stored_session.profile == "ops"
    assert snapshot_session.messages == [{"role": "user", "content": RETAGGED_SECRET}]


# ───────────────────────────────────────────────────────────────────────────
# Round 5 maintainer review (PR #6889)
#
# 1. Share create/revoke ran the GENERIC request guard
#    (_guard_request_session_visibility -> _session_id_visible_to_request_profile),
#    a plain profile match with no agnostic exemption, BEFORE the share
#    resolver. The first share of an unstored Claude Code transcript passed
#    (get_session raised KeyError) and persisted a `profile: None` sidecar;
#    every later revoke/refresh from that same profile 404'd, stranding a
#    public link the user could not take down.
# 2. Isolated mode answered 403 (read-only) instead of 404 for a hidden
#    transcript on the materialize + branch paths, revealing its existence.
# 3. Search and export skipped the isolated id-prefix rule.
# 4. The share snapshot read state.db under the sidecar's `None` profile while
#    authorizing on the CLI metadata row's profile.
# ───────────────────────────────────────────────────────────────────────────


class _PostHandler:
    """Minimal stand-in for the BaseHTTPRequestHandler in handle_post()."""

    command = "POST"

    def __init__(self):
        self.headers = {}
        self.client_address = ("127.0.0.1", 0)


def _stored_claude_code_sidecar(*, profile=None, messages=None):
    """A persisted WebUI sidecar for a Claude Code transcript.

    Real ``Session`` (not a MagicMock): the share handlers call ``compact()``
    and ``copy.copy()`` on it. ``save`` is neutered so the test never writes to
    the sidecar store.
    """
    session = Session(
        session_id=CLAUDE_SID,
        title="Claude Code transcript",
        workspace="/home/user/project",
        model="claude-code",
        messages=(
            messages
            if messages is not None
            else [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        ),
        profile=profile,
        is_cli_session=True,
        source_tag="claude_code",
        raw_source="claude_code",
        session_source="external_agent",
        source_label="Claude Code",
        read_only=True,
    )
    session.save = lambda **_kwargs: None
    return session


def _patch_share_post(
    monkeypatch,
    *,
    session,
    cli_meta,
    active_profile,
    isolated=False,
):
    """Wire handle_post() for a share round trip and return the capture dict."""
    cap = _capture(monkeypatch)
    revoked = []

    def _get_session(_sid, metadata_only=False):
        if session is None:
            raise KeyError(_sid)
        return session

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata", lambda *_a, **_kw: dict(cli_meta or {})
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: isolated)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        routes,
        "create_or_refresh_share",
        lambda snapshot: {
            "share_token": "tok-123",
            "share_created_at": 100.0,
            "share_updated_at": 100.0,
            "share_title": getattr(snapshot, "title", ""),
            "share_message_count": len(getattr(snapshot, "messages", None) or []),
        },
    )
    monkeypatch.setattr(routes, "revoke_share", lambda target: revoked.append(target))
    cap["revoked"] = revoked
    return cap


def _post(monkeypatch, path, body):
    monkeypatch.setattr(routes, "read_body", lambda _handler: dict(body))
    return routes.handle_post(_PostHandler(), urlparse(path))


def test_share_create_then_revoke_through_handle_post_under_named_profile(monkeypatch):
    """The share a named profile creates must stay revocable from that profile.

    Drives the real POST entry point so the generic request-visibility guard
    runs. Before the fix the guard 404'd both calls (stored sidecar profile is
    None, active profile is `feng-family`) before `_resolve_share_session_pair`
    ever ran — and the create only worked the very first time, while no sidecar
    existed yet, which is exactly how an unrevokable public link was minted.
    """
    row = _claude_code_row()
    session = _stored_claude_code_sidecar()
    cap = _patch_share_post(
        monkeypatch, session=session, cli_meta=row, active_profile="feng-family"
    )

    assert _post(monkeypatch, "/api/share/create", {"session_id": CLAUDE_SID}) is True
    assert cap.get("error") is None
    assert cap["status"] == 200
    assert cap["data"]["share"]["token"] == "tok-123"
    assert session.share_token == "tok-123"

    assert _post(monkeypatch, "/api/share/revoke", {"session_id": CLAUDE_SID}) is True
    assert cap.get("error") is None
    assert cap["status"] == 200
    assert cap["revoked"] == [session]
    assert session.share_token is None
    assert session.share_created_at is None
    assert cap["data"]["session"]["share_token"] is None


def test_share_post_guard_defers_to_the_share_resolver():
    """The generic request guard is exempt for both share routes."""
    assert routes._request_session_visibility_exempt("POST", "/api/share/create") is True
    assert routes._request_session_visibility_exempt("POST", "/api/share/revoke") is True
    # The exemption is POST-only and path-exact.
    assert routes._request_session_visibility_exempt("GET", "/api/share/create") is False
    assert routes._request_session_visibility_exempt("POST", "/api/share") is False
    assert routes._request_session_visibility_exempt("POST", "/api/session/delete") is False


def test_share_create_through_handle_post_still_404s_under_isolation(monkeypatch):
    """The guard exemption must not open an isolated-mode hole."""
    row = _claude_code_row()
    session = _stored_claude_code_sidecar()
    cap = _patch_share_post(
        monkeypatch,
        session=session,
        cli_meta=row,
        active_profile="default",
        isolated=True,
    )

    assert _post(monkeypatch, "/api/share/create", {"session_id": CLAUDE_SID}) is True
    assert cap["status"] == 404
    assert cap["error"] == "Session not found"
    assert session.share_token is None


def test_share_create_through_handle_post_still_404s_for_a_foreign_owner(monkeypatch):
    """A sidecar owned by another profile stays unshareable after the exemption."""
    row = _claude_code_row()
    session = _stored_claude_code_sidecar(profile="other")
    cap = _patch_share_post(
        monkeypatch, session=session, cli_meta=row, active_profile="feng-family"
    )

    assert _post(monkeypatch, "/api/share/create", {"session_id": CLAUDE_SID}) is True
    assert cap["status"] == 404
    assert cap["error"] == "Session not found"
    assert session.share_token is None


def test_share_revoke_through_handle_post_still_404s_for_a_foreign_owner(monkeypatch):
    row = _claude_code_row()
    session = _stored_claude_code_sidecar(profile="other")
    session.share_token = "tok-foreign"
    cap = _patch_share_post(
        monkeypatch, session=session, cli_meta=row, active_profile="feng-family"
    )

    assert _post(monkeypatch, "/api/share/revoke", {"session_id": CLAUDE_SID}) is True
    assert cap["status"] == 404
    assert cap["error"] == "Session not found"
    assert cap["revoked"] == []
    assert session.share_token == "tok-foreign"


# ── Item 2: isolated mode must not confirm a hidden transcript exists ───────


def test_isolated_materialize_reports_hidden_transcript_missing_not_readonly(monkeypatch):
    """/api/chat/start 403'd ("read-only imported"), which proves existence."""
    reads = []

    def _get_session(_sid, metadata_only=False):
        reads.append(("get_session", _sid))
        raise KeyError(_sid)

    def _cli_meta(_sid, **_kw):
        reads.append(("cli_meta", _sid))
        return _claude_code_row()

    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", _cli_meta)

    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    with pytest.raises(KeyError):
        routes._get_or_materialize_session(CLAUDE_SID)
    # Decided from the id: neither the sidecar store nor the metadata cache
    # was consulted for a transcript this deployment must not see.
    assert reads == []

    # Negative control: the same input outside isolation still reports the
    # read-only refusal (403), so the 404 above is the isolation rule and not a
    # blanket regression.
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    with pytest.raises(PermissionError):
        routes._get_or_materialize_session(CLAUDE_SID)
    assert ("cli_meta", CLAUDE_SID) in reads


def test_isolated_branch_reports_hidden_transcript_missing_not_readonly(monkeypatch):
    """POST /api/session/branch answered 403 for a hidden Claude Code transcript."""
    cap = _capture(monkeypatch)
    synth = _synth_for(_claude_code_row())

    def _get_session(_sid, metadata_only=False):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(
        routes, "_claim_or_synthesize_cli_session", lambda _sid, **_kw: (synth, "not_claimable")
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")

    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    assert _post(monkeypatch, "/api/session/branch", {"session_id": CLAUDE_SID}) is True
    assert (cap["error"], cap["status"]) == ("Session not found", 404)

    # Negative control: outside isolation the read-only refusal still applies.
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    assert _post(monkeypatch, "/api/session/branch", {"session_id": CLAUDE_SID}) is True
    assert cap["status"] == 403
    assert cap["error"] == "Read-only sessions cannot be branched from WebUI"


# ── Item 3: search and export owe the isolated id-prefix rule ───────────────


def _search(monkeypatch, *, rows, active_profile, isolated, query="secret"):
    cap = _capture(monkeypatch)
    monkeypatch.setattr(routes, "all_sessions", lambda *_a, **_kw: [dict(r) for r in rows])
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: isolated)
    monkeypatch.setattr(routes, "load_settings", lambda *_a, **_kw: {})
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile)
    routes._handle_sessions_search(
        _PostHandler(), urlparse(f"/api/sessions/search?q={query}&content=0")
    )
    return cap


def test_isolated_search_drops_profile_agnostic_rows(monkeypatch):
    """A stored claude_code_* sidecar was title-searchable under isolation."""
    row = dict(_claude_code_row(), title="secret claude transcript", profile="default")
    cap = _search(monkeypatch, rows=[row], active_profile="default", isolated=True)
    assert cap["data"]["sessions"] == []


def test_search_keeps_profile_agnostic_rows_under_a_named_profile(monkeypatch):
    """Negative control: the sidebar rule also widens search outside isolation."""
    row = dict(_claude_code_row(), title="secret claude transcript")
    cap = _search(monkeypatch, rows=[row], active_profile="feng-family", isolated=False)
    assert [s["session_id"] for s in cap["data"]["sessions"]] == [CLAUDE_SID]


def test_search_still_scopes_profile_tagged_rows(monkeypatch):
    """Negative control: an ordinary row owned by another profile stays hidden."""
    row = {
        "session_id": "20260101_000000_abc123",
        "title": "secret other-profile session",
        "profile": "other",
    }
    cap = _search(monkeypatch, rows=[row], active_profile="feng-family", isolated=False)
    assert cap["data"]["sessions"] == []


def test_isolated_export_404s_profile_agnostic_session(monkeypatch):
    """A stored claude_code_* sidecar was exportable while detail 404'd it."""
    cap = _capture(monkeypatch)
    reads = []

    def _get_session(_sid, metadata_only=False):
        reads.append(_sid)
        return _stored_claude_code_sidecar(profile="default")

    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    assert (
        routes._handle_session_export(
            _PostHandler(), urlparse(f"/api/session/export?session_id={CLAUDE_SID}")
        )
        is True
    )
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    # Rejected from the id, before the sidecar store is read.
    assert reads == []


def test_export_still_serves_an_ordinary_session_under_isolation(monkeypatch):
    """Negative control: the id gate only covers agnostic transcripts."""
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    assert routes._is_profile_agnostic_session_id("20260101_000000_abc123") is False


# ── Item 4: the share snapshot must read the profile it authorized under ────


def test_share_snapshot_reads_the_authorized_profile_database(monkeypatch):
    """Authorizing on cli_meta.profile but reading with session.profile (None)
    snapshotted from the wrong profile database."""
    row = dict(_claude_code_row(), profile="feng-family")
    stored = _stored_claude_code_sidecar(profile=None, messages=[])
    seen = {}

    def _cli_messages(sid, profile=None, **_kw):
        seen["sid"] = sid
        seen["profile"] = profile
        return [{"role": "user", "content": "from feng-family state.db"}]

    with (
        patch("api.routes.get_session", return_value=stored),
        patch("api.routes._get_active_profile_name", return_value="feng-family"),
        patch("api.routes._lookup_cli_session_metadata", return_value=row),
        patch("api.routes._is_isolated_profile_mode", return_value=False),
        patch("api.routes.get_cli_session_messages", _cli_messages),
    ):
        snapshot, stored_session, cli_meta = routes._resolve_share_session_pair(
            CLAUDE_SID, MagicMock()
        )

    assert seen == {"sid": CLAUDE_SID, "profile": "feng-family"}
    assert snapshot.messages == [{"role": "user", "content": "from feng-family state.db"}]
    assert stored_session is stored
    assert cli_meta["profile"] == "feng-family"


def test_share_effective_profile_prefers_the_stored_owner():
    stored = _stored_claude_code_sidecar(profile="ops")
    assert routes._share_effective_profile(stored, {"profile": "feng-family"}) == "ops"
    assert routes._share_effective_profile(_stored_claude_code_sidecar(), {"profile": "ops"}) == "ops"
    assert routes._share_effective_profile(_stored_claude_code_sidecar(), {}) is None
    assert routes._share_effective_profile(None, None) is None


def test_share_snapshot_falls_back_to_the_session_profile(monkeypatch):
    """Direct callers without an effective profile keep the historical read."""
    stored = _stored_claude_code_sidecar(profile="ops", messages=[])
    seen = {}

    def _cli_messages(sid, profile=None, **_kw):
        seen["profile"] = profile
        return [{"role": "user", "content": "ops transcript"}]

    monkeypatch.setattr(routes, "get_cli_session_messages", _cli_messages)
    assert routes._share_snapshot_messages_for_session(stored, cli_meta={}) == [
        {"role": "user", "content": "ops transcript"}
    ]
    assert seen["profile"] == "ops"


# ── Shared source-tag registry (one set behind both agnostic predicates) ────


def test_profile_agnostic_source_tags_is_claude_code_only():
    """The speculative Codex arm is gone: api/codex_sessions.py does not exist.

    Both agnostic predicates read this one set, so the row shape and the id
    prefix can never disagree about which stores sit outside the profile tree.
    """
    import sys

    assert "api.codex_sessions" not in sys.modules
    assert routes._profile_agnostic_source_tags() == frozenset({"claude_code"})
    assert routes._is_profile_agnostic_session_id("codex_session_123") is False
    assert (
        routes._is_profile_agnostic_foreign_session(
            {
                "profile": None,
                "read_only": True,
                "session_source": "external_agent",
                "source_tag": "codex",
                "raw_source": "codex",
            }
        )
        is False
    )
    # ...and the tag it does carry still matches the row predicate.
    assert routes._is_profile_agnostic_foreign_session(_claude_code_row()) is True


# ───────────────────────────────────────────────────────────────────────────
# Round 5 review 2: guard ORDER under isolation
#
# _guard_request_session_visibility() runs on every /api/ request before the
# per-endpoint gates. Those gates already answer a non-disclosing 404 from the
# id under isolation (_load_branch_source_or_refuse, _handle_session_export),
# but the generic guard reached them first and, for a stored claude_code_*
# sidecar carrying some OTHER profile, answered 409 session_profile_mismatch —
# publishing both that the hidden transcript exists and which profile owns it.
# The guard owes the same id rule, applied before the sidecar store is read.
# ───────────────────────────────────────────────────────────────────────────


def _guard(monkeypatch, parsed, *, body=None, method="GET", session, isolated, active="ops"):
    """Run the generic request guard and return (allowed, capture, reads)."""
    cap = _capture(monkeypatch)
    reads = []

    def _get_session(_sid, metadata_only=False):
        reads.append(_sid)
        if session is None:
            raise KeyError(_sid)
        return session

    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: isolated)
    allowed = routes._guard_request_session_visibility(
        MagicMock(), parsed, body=body, method=method
    )
    return allowed, cap, reads


def test_branch_generic_guard_404s_isolated_agnostic_id_instead_of_409(monkeypatch):
    """POST body session_id: 409 mismatch disclosed the hidden transcript."""
    allowed, cap, reads = _guard(
        monkeypatch,
        urlparse("/api/session/branch"),
        body={"session_id": CLAUDE_SID},
        method="POST",
        session=_stored_claude_code_sidecar(profile="other"),
        isolated=True,
    )
    assert allowed is False
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    assert "code" not in (cap.get("data") or {})
    # Decided from the id: the sidecar store was never read, so a stale row
    # cannot leak the owning profile name either.
    assert reads == []


def test_export_generic_guard_404s_isolated_agnostic_id_instead_of_409(monkeypatch):
    """Query-string session_id on the GET path takes the same rule."""
    allowed, cap, reads = _guard(
        monkeypatch,
        urlparse(f"/api/session/export?session_id={CLAUDE_SID}"),
        method="GET",
        session=_stored_claude_code_sidecar(profile="other"),
        isolated=True,
    )
    assert allowed is False
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    assert reads == []


def test_isolated_guard_404s_an_agnostic_id_with_no_sidecar_at_all(monkeypatch):
    """A cold sidecar store must not fall through to the endpoint either."""
    allowed, cap, _reads = _guard(
        monkeypatch,
        urlparse("/api/session/delete"),
        body={"session_id": CLAUDE_SID},
        method="POST",
        session=None,
        isolated=True,
    )
    assert allowed is False
    assert (cap["error"], cap["status"]) == ("Session not found", 404)


def test_guard_keeps_the_409_contract_outside_isolation(monkeypatch):
    """Negative control: the id rule is isolation-only.

    Outside isolation a sidecar owned by a known other profile still gets the
    #5419 409 so the client can offer to switch to that profile.
    """
    allowed, cap, reads = _guard(
        monkeypatch,
        urlparse("/api/session/branch"),
        body={"session_id": CLAUDE_SID},
        method="POST",
        session=_stored_claude_code_sidecar(profile="other"),
        isolated=False,
    )
    assert allowed is False
    assert cap["status"] == 409
    assert cap["data"]["code"] == "session_profile_mismatch"
    assert cap["data"]["profile"] == "other"
    assert reads == [CLAUDE_SID]


def test_guard_keeps_the_409_contract_for_ordinary_ids_under_isolation(monkeypatch):
    """Negative control: the id rule does not widen to profile-tree sessions."""
    ordinary = Session(session_id="20260101_000000_abc123", profile="other")
    ordinary.save = lambda **_kwargs: None
    allowed, cap, reads = _guard(
        monkeypatch,
        urlparse("/api/session/branch"),
        body={"session_id": "20260101_000000_abc123"},
        method="POST",
        session=ordinary,
        isolated=True,
    )
    assert allowed is False
    assert cap["status"] == 409
    assert cap["data"]["code"] == "session_profile_mismatch"
    assert reads == ["20260101_000000_abc123"]


def test_guard_still_admits_a_matching_session_under_isolation(monkeypatch):
    """Negative control: an in-profile ordinary session is untouched."""
    ordinary = Session(session_id="20260101_000000_abc123", profile="ops")
    ordinary.save = lambda **_kwargs: None
    allowed, cap, _reads = _guard(
        monkeypatch,
        urlparse("/api/session/branch"),
        body={"session_id": "20260101_000000_abc123"},
        method="POST",
        session=ordinary,
        isolated=True,
    )
    assert allowed is True
    assert cap == {}


def test_isolated_branch_404s_end_to_end_for_a_foreign_owned_sidecar(monkeypatch):
    """Full POST stack: guard first, endpoint gate second, one 404 either way."""
    cap = _capture(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: _stored_claude_code_sidecar(profile="other"),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    assert _post(monkeypatch, "/api/session/branch", {"session_id": CLAUDE_SID}) is True
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    assert cap.get("data") is None


def test_isolated_export_404s_end_to_end_for_a_foreign_owned_sidecar(monkeypatch):
    """Full GET stack for export: no 409, no transcript."""
    cap = _capture(monkeypatch)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: _stored_claude_code_sidecar(profile="other"),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    allowed = routes._guard_request_session_visibility(
        MagicMock(),
        urlparse(f"/api/session/export?session_id={CLAUDE_SID}"),
        method="GET",
    )
    assert allowed is False
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    # And the endpoint itself repeats the verdict if it is ever reached
    # directly (e.g. a future caller that bypasses the generic guard).
    assert (
        routes._handle_session_export(
            _PostHandler(), urlparse(f"/api/session/export?session_id={CLAUDE_SID}")
        )
        is True
    )
    assert (cap["error"], cap["status"]) == ("Session not found", 404)


# ───────────────────────────────────────────────────────────────────────────
# Round 6 maintainer review (PR #6889)
#
# The item-2 fix made `_get_or_materialize_session()` raise KeyError for an
# isolated profile-agnostic id. POST /api/chat/start catches exactly that
# KeyError and reads it as "no WebUI sidecar exists", so it fell through to
# `_claim_or_synthesize_cli_session()` — which reads the Claude Code JSONL and
# returns a claimable Session the handler then `.save()`s. With a stored
# read_only=True sidecar on disk and a cold/stale CLI metadata cache the
# request still answered 404, but the sidecar was rewritten: read_only cleared
# and the original message replaced by the external transcript. The isolation
# rule has to be decided before that fallback can run.
# ───────────────────────────────────────────────────────────────────────────


STORED_SIDECAR_MESSAGE = "sidecar message that must survive the 404"
HIDDEN_TRANSCRIPT_TEXT = "isolated deployment must never read this"


def _real_claude_code_transcript(tmp_path, monkeypatch):
    """Write a real Claude Code JSONL and return its scanner-derived sid."""
    import json
    import api.models as models

    projects_dir = tmp_path / "claude" / "projects"
    session_file = projects_dir / "proj" / "session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"summary": "Hidden Claude Code transcript"},
        {
            "timestamp": "2026-04-18T12:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": HIDDEN_TRANSCRIPT_TEXT}],
            },
        },
    ]
    session_file.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", str(projects_dir))

    sid = models._claude_code_session_id(session_file)
    assert sid.startswith("claude_code_")
    # The transcript really is readable from disk, so a 404 below is the
    # isolation rule and not a missing file.
    disk_msgs = models.get_claude_code_session_messages(sid, projects_dir=projects_dir)
    assert [m["content"] for m in disk_msgs] == [HIDDEN_TRANSCRIPT_TEXT]
    return sid


def _stored_read_only_sidecar_on_disk(tmp_path, monkeypatch, sid):
    """Persist a real read_only=True sidecar and return (path, bytes)."""
    import api.models as models

    session_dir = tmp_path / "webui-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.pop(sid, None)

    sidecar = Session(
        session_id=sid,
        title="Claude Code transcript",
        workspace="/home/user/project",
        model="claude-code",
        messages=[{"role": "user", "content": STORED_SIDECAR_MESSAGE}],
        profile=None,
        is_cli_session=True,
        source_tag="claude_code",
        raw_source="claude_code",
        session_source="external_agent",
        source_label="Claude Code",
        read_only=True,
    )
    sidecar.save()
    path = session_dir / f"{sid}.json"
    assert path.exists()
    return path, path.read_bytes()


def test_isolated_chat_start_404s_without_claiming_the_read_only_sidecar(
    monkeypatch, tmp_path
):
    """POST /api/chat/start must not rewrite a hidden transcript's sidecar.

    Isolated named profile + a real ``claude_code_*`` JSONL on disk + a cold
    (stale) CLI metadata cache + a stored ``read_only=True`` sidecar. Before the
    fix the response was a correct 404 while the fallback claimed the session:
    the sidecar came back ``read_only=False`` with the external transcript in
    place of its own message.
    """
    import api.models as models

    sid = _real_claude_code_transcript(tmp_path, monkeypatch)
    sidecar_path, sidecar_bytes = _stored_read_only_sidecar_on_disk(
        tmp_path, monkeypatch, sid
    )

    cap = _capture(monkeypatch)
    calls = {"transcript_reads": 0, "claims": 0, "runs": 0, "sidecar_loads": 0}

    def _get_session(_sid, metadata_only=False):
        calls["sidecar_loads"] += 1
        loaded = models.Session.load(_sid)
        if loaded is None:
            raise KeyError(_sid)
        return loaded

    def _get_cli_session_messages(_sid, *_a, **_kw):
        calls["transcript_reads"] += 1
        return models.get_claude_code_session_messages(_sid)

    _real_claim = routes._claim_or_synthesize_cli_session

    def _counting_claim(_sid, **kwargs):
        calls["claims"] += 1
        return _real_claim(_sid, **kwargs)

    def _unexpected_run(*_a, **_kw):
        calls["runs"] += 1
        return {"ok": True}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(routes, "get_cli_session_messages", _get_cli_session_messages)
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", _counting_claim)
    monkeypatch.setattr(routes, "_start_chat_stream_for_session", _unexpected_run)
    # Cold/stale metadata cache: the JSONL is on disk but was never scanned, so
    # the metadata-shaped gates read it as "not a profile-agnostic row".
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_kw: {})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    assert (
        _post(
            monkeypatch,
            "/api/chat/start",
            {"session_id": sid, "message": "continue this hidden transcript"},
        )
        is True
    )

    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    assert cap.get("data") is None
    # The stored sidecar is untouched, byte for byte.
    assert sidecar_path.read_bytes() == sidecar_bytes
    reloaded = models.Session.load(sid)
    assert reloaded.read_only is True
    assert reloaded.messages == [{"role": "user", "content": STORED_SIDECAR_MESSAGE}]
    # Decided from the id: no transcript read, no claim, no run.
    assert calls == {
        "transcript_reads": 0,
        "claims": 0,
        "runs": 0,
        "sidecar_loads": 0,
    }


def test_isolated_claim_helper_refuses_a_hidden_transcript(monkeypatch, tmp_path):
    """Chokepoint guard: the claim helper itself never materializes a hidden id.

    Every current caller gates the id before reaching the helper; this pins the
    refusal at the one place that reads the external JSONL, so a future
    raise-KeyError-then-claim caller cannot reopen the same hole.
    """
    import api.models as models

    sid = _real_claude_code_transcript(tmp_path, monkeypatch)
    reads = []

    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda _sid, *_a, **_kw: reads.append(_sid)
        or models.get_claude_code_session_messages(_sid),
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_kw: {})

    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    assert routes._claim_or_synthesize_cli_session(sid) == (None, "isolated_hidden")
    assert reads == []

    # Negative control: outside isolation the same input still materializes,
    # so the refusal above is the isolation rule and not a blanket regression.
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    synth, reason = routes._claim_or_synthesize_cli_session(sid)
    assert reason == "materialized"
    assert synth is not None
    assert reads == [sid]


def test_chat_start_still_claims_a_non_agnostic_session_under_isolation(
    monkeypatch, tmp_path
):
    """Negative control: the KeyError claim path is intact for ordinary ids.

    Same isolated named profile, but an in-profile id: the new gate keys on the
    profile-agnostic id prefix only, so the TUI/Desktop claim contract (#4911)
    still reaches ``_claim_or_synthesize_cli_session()`` and persists its
    sidecar.
    """
    import api.models as models

    session_dir = tmp_path / "webui-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    cap = _capture(monkeypatch)
    claimed = Session(
        session_id="20260101_000000_abc123",
        title="TUI session",
        workspace=os.path.expanduser("~"),
        model="unknown",
        messages=[{"role": "user", "content": "from the TUI"}],
        profile="ops",
        is_cli_session=True,
        source_tag="tui",
        raw_source="tui",
    )
    started = []

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: (_ for _ in ()).throw(KeyError(_sid)),
    )
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda _sid, **_kw: (claimed, "materialized"),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda *_a, **_kw: started.append(True) or {"ok": True, "stream_id": "s1"},
    )

    assert (
        _post(
            monkeypatch,
            "/api/chat/start",
            {"session_id": claimed.session_id, "message": "hello"},
        )
        is True
    )
    assert cap.get("error") is None
    assert (session_dir / f"{claimed.session_id}.json").exists()
    assert started == [True]


def test_isolated_chat_start_still_suppresses_a_silent_control_message(
    monkeypatch, tmp_path
):
    """`[SILENT]` stays an unconditional 200 no-op, even for a hidden id.

    The sentinel is control-plane traffic, not conversation: `tests/
    test_silent_control_suppression.py` pins it as suppressed *before* any
    session lookup or pending-state mutation. Ordering the isolation gate ahead
    of that check turned an isolated `claude_code_*` POST into a 404, which a
    wake relay reads as "session is gone" instead of "delivery suppressed".
    Since suppression returns before any lookup, there is no hidden transcript
    left for the isolation rule to protect here.
    """
    import api.models as models

    sid = _real_claude_code_transcript(tmp_path, monkeypatch)
    sidecar_path, sidecar_bytes = _stored_read_only_sidecar_on_disk(
        tmp_path, monkeypatch, sid
    )

    cap = _capture(monkeypatch)
    calls = {"transcript_reads": 0, "claims": 0, "runs": 0, "sidecar_loads": 0}

    def _unexpected_lookup(_sid, metadata_only=False):
        calls["sidecar_loads"] += 1
        raise AssertionError("[SILENT] must be suppressed before session lookup")

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "get_session", _unexpected_lookup)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda *_a, **_kw: calls.__setitem__(
            "transcript_reads", calls["transcript_reads"] + 1
        ),
    )
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda *_a, **_kw: calls.__setitem__("claims", calls["claims"] + 1),
    )
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda *_a, **_kw: calls.__setitem__("runs", calls["runs"] + 1),
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_kw: {})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    assert (
        _post(monkeypatch, "/api/chat/start", {"session_id": sid, "message": "  [SILENT]\n"})
        is True
    )

    assert cap.get("error") is None, (
        "the [SILENT] sentinel must stay a 200 no-op under isolation, not 404"
    )
    assert cap["status"] == 200
    assert cap["data"] == {
        "status": "suppressed",
        "reason": "silent_control_message",
    }
    # Suppressed means nothing was read, claimed, run or written.
    assert calls == {
        "transcript_reads": 0,
        "claims": 0,
        "runs": 0,
        "sidecar_loads": 0,
    }
    assert sidecar_path.read_bytes() == sidecar_bytes
    reloaded = models.Session.load(sid)
    assert reloaded.read_only is True
    assert reloaded.messages == [{"role": "user", "content": STORED_SIDECAR_MESSAGE}]


def test_isolated_chat_start_404s_a_non_sentinel_message_that_merely_contains_silent(
    monkeypatch, tmp_path
):
    """Negative control: only the exact sentinel escapes the isolation gate.

    `_is_silent_control_message` matches exact, case-sensitive, whitespace-
    stripped `[SILENT]`. Ordinary text that merely mentions it is conversation
    content, so the hidden-transcript rule still applies and answers 404.
    """
    sid = _real_claude_code_transcript(tmp_path, monkeypatch)
    _stored_read_only_sidecar_on_disk(tmp_path, monkeypatch, sid)

    cap = _capture(monkeypatch)

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: (_ for _ in ()).throw(
            AssertionError("isolated hidden id must 404 before session lookup")
        ),
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_kw: {})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)

    assert (
        _post(
            monkeypatch,
            "/api/chat/start",
            {"session_id": sid, "message": "why did you emit [SILENT] earlier?"},
        )
        is True
    )
    assert (cap["error"], cap["status"]) == ("Session not found", 404)
    assert cap.get("data") is None
