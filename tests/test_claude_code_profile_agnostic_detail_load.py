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
    parsed = urlparse("/api/session?session_id=%s&messages=0&resolve_model=0" % CLAUDE_SID)

    with patch("api.routes.get_session", side_effect=KeyError(CLAUDE_SID)), \
         patch("api.routes._get_active_profile_name", return_value="feng-family"), \
         patch("api.routes._lookup_cli_session_metadata", return_value=row), \
         patch(
             "api.routes._claim_or_synthesize_cli_session",
             return_value=(synth, "not_claimable"),
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
    parsed = urlparse("/api/session?session_id=%s&messages=0&resolve_model=0" % row["session_id"])

    with patch("api.routes.get_session", side_effect=KeyError(row["session_id"])), \
         patch("api.routes._get_active_profile_name", return_value="feng-family"), \
         patch("api.routes._lookup_cli_session_metadata", return_value=row):
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
    # Missing / empty metadata is never exempt.
    assert routes._is_profile_agnostic_foreign_session({}) is False
    assert routes._is_profile_agnostic_foreign_session(None) is False
