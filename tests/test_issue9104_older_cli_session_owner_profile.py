"""#9104 item 1: opening an OLDER CLI session by id under a named profile.

``_lookup_cli_session_metadata`` only returns a row for the newest-N CLI
sessions (plus a by-id fallback for a few sources). An older session in a
named profile has no row at all, so the ``GET /api/session`` foreign-session
branch computed ``_session_profile=None``; ``_session_visible_to_active_profile``
coerces ``None`` to the root profile via ``_profiles_match`` and the request
was answered 404 before ``_claim_or_synthesize_cli_session`` could stamp the
session with its real owner.

The fix mirrors :func:`api.routes._cli_read_owner_profile` (added for import
and claim in #9100): without a read_profile, ``get_cli_session_messages``
reads the ACTIVE profile's state.db, so messages found there mean the session
belongs to the active profile. The same gate on the share path
(``_resolve_share_session_pair``) gets the same fallback.

Pinned behaviour:
- older session, no listing row, messages in the active profile's state.db
  → detail load reaches the claim path (200);
- no row AND no messages there → still 404 (fail closed, self-heal intact);
- a listing row that names another profile → still 409
  ``session_profile_mismatch`` (the fallback must not override an explicit tag);
- profile-agnostic Claude Code rows keep bypassing the gate;
- under the root profile nothing changes (the gate already passed);
- an unreadable state.db (lookup raises) → still 404, never treated visible.

"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.routes as routes
from api.models import Session


OLD_SID = "20250101_120000_deadbeef"


def _transcript():
    return [
        {"role": "user", "content": "older question", "timestamp": 1.0},
        {"role": "assistant", "content": "older answer", "timestamp": 2.0},
    ]


def _synth(sid, profile="feng-family"):
    return Session(
        session_id=sid,
        title="CLI Session",
        workspace="/tmp",
        model="unknown",
        messages=_transcript(),
        created_at=1.0,
        updated_at=2.0,
        profile=profile,
        is_cli_session=True,
        read_only=False,
    )


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


def _detail_load(monkeypatch, sid, *, active_profile, cli_meta, messages, claim=None, claim_error=None):
    """Drive ``GET /api/session`` for a foreign session with no sidecar.

    Returns ``(capture, claim_calls)``; ``claim_calls`` counts how many times
    the request reached ``_claim_or_synthesize_cli_session``. ``claim``
    overrides the synthesized session the fake claim path returns.
    """
    cap = _capture(monkeypatch)
    handler = MagicMock()
    parsed = urlparse("/api/session?session_id=%s&messages=0&resolve_model=0" % sid)
    calls = {"claim": 0}

    def fake_claim(sid_, cli_meta=None, *, cli_messages=None):
        calls["claim"] += 1
        if claim_error is not None:
            raise claim_error
        if claim is None:
            return _synth(sid_), "materialized"
        return claim

    with patch("api.routes.get_session", side_effect=KeyError(sid)), \
         patch("api.routes._get_active_profile_name", return_value=active_profile), \
         patch("api.routes._lookup_cli_session_metadata", return_value=cli_meta), \
         patch("api.routes.get_cli_session_messages", return_value=messages), \
         patch("api.routes._claim_or_synthesize_cli_session", side_effect=fake_claim):
        assert routes.handle_get(handler, parsed) is True
    return cap, calls


def _share_pair(sid, *, active_profile, cli_meta, messages):
    """Drive ``_resolve_share_session_pair`` for a foreign session with no sidecar."""
    handler = MagicMock()
    with patch("api.routes.get_session", side_effect=KeyError(sid)), \
         patch("api.routes._get_active_profile_name", return_value=active_profile), \
         patch("api.routes._lookup_cli_session_metadata", return_value=cli_meta), \
         patch("api.routes.get_cli_session_messages", return_value=messages), \
         patch(
             "api.routes._claim_or_synthesize_cli_session",
             return_value=(_synth(sid), "materialized"),
         ):
        return routes._resolve_share_session_pair(sid, handler)


# ---------------------------------------------------------------------------
# The fix: older CLI session, no listing row, messages in the active profile
# ---------------------------------------------------------------------------


def test_older_cli_session_without_listing_row_opens_under_named_profile(monkeypatch):
    cap, calls = _detail_load(
        monkeypatch,
        OLD_SID,
        active_profile="feng-family",
        cli_meta={},  # no listing row: older than the newest-N window
        messages=_transcript(),
    )
    assert cap.get("error") is None, (
        "an older CLI session whose messages live in the active profile's "
        "state.db must not be 404'd under a named profile"
    )
    assert cap["status"] == 200
    sess = cap["data"]["session"]
    assert sess["session_id"] == OLD_SID
    assert calls["claim"] == 1, "the request must reach _claim_or_synthesize_cli_session"


def test_share_path_opens_older_cli_session_under_named_profile():
    snapshot, stored, cli_meta = _share_pair(
        OLD_SID,
        active_profile="feng-family",
        cli_meta={},
        messages=_transcript(),
    )
    assert stored is None
    assert snapshot.session_id == OLD_SID
    assert cli_meta == {}


# ---------------------------------------------------------------------------
# Fail-closed regressions: the fallback must not open unknown sessions
# ---------------------------------------------------------------------------


def test_session_absent_from_active_profile_state_db_still_404s(monkeypatch):
    cap, calls = _detail_load(
        monkeypatch,
        OLD_SID,
        active_profile="feng-family",
        cli_meta={},
        messages=[],  # not in the active profile's state.db either
    )
    assert cap["status"] == 404
    assert cap["error"] == "Session not found"
    assert calls["claim"] == 0


def test_share_path_refuses_session_absent_from_active_profile_state_db():
    with pytest.raises(KeyError):
        _share_pair(
            OLD_SID,
            active_profile="feng-family",
            cli_meta={},
            messages=[],
        )


def test_unreadable_state_db_fails_closed(monkeypatch):
    cap = _capture(monkeypatch)
    handler = MagicMock()
    parsed = urlparse("/api/session?session_id=%s&messages=0&resolve_model=0" % OLD_SID)
    claim_calls = {"n": 0}

    def exploding_messages(_sid, **_kw):
        raise RuntimeError("state.db unreadable")

    with patch("api.routes.get_session", side_effect=KeyError(OLD_SID)), \
         patch("api.routes._get_active_profile_name", return_value="feng-family"), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_cli_session_messages", side_effect=exploding_messages), \
         patch(
             "api.routes._claim_or_synthesize_cli_session",
             side_effect=lambda *_a, **_kw: claim_calls.__setitem__("n", claim_calls["n"] + 1),
         ):
        assert routes.handle_get(handler, parsed) is True

    assert cap["status"] == 404
    assert claim_calls["n"] == 0, "an unreadable state.db must never be treated as visible"


# ---------------------------------------------------------------------------
# Regression controls: existing gates keep their behaviour
# ---------------------------------------------------------------------------


def test_listing_row_naming_another_profile_still_409s(monkeypatch):
    row = {
        "session_id": OLD_SID,
        "title": "CLI Session",
        "profile": "other-profile",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
    }
    cap, calls = _detail_load(
        monkeypatch,
        OLD_SID,
        active_profile="feng-family",
        cli_meta=row,
        # Even with messages in the active profile's state.db, an explicit
        # other-profile tag must win over the ownership fallback.
        messages=_transcript(),
    )
    assert cap["status"] == 409
    assert cap["data"]["code"] == "session_profile_mismatch"
    assert cap["data"]["profile"] == "other-profile"
    assert calls["claim"] == 0


def test_share_path_keeps_409_style_refusal_for_other_profile_tag():
    row = {
        "session_id": OLD_SID,
        "title": "CLI Session",
        "profile": "other-profile",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
    }
    with pytest.raises(KeyError):
        _share_pair(
            OLD_SID,
            active_profile="feng-family",
            cli_meta=row,
            messages=_transcript(),
        )


def test_claude_code_profile_agnostic_row_still_opens_under_named_profile(monkeypatch):
    claude_sid = "claude_code_491fbe3e6ea1248d70a4177f"
    row = {
        "session_id": claude_sid,
        "title": "Claude Code transcript",
        "workspace": "/home/user/project",
        "model": "claude-code",
        "created_at": 1.0,
        "updated_at": 2.0,
        "profile": None,
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
    }
    cap, calls = _detail_load(
        monkeypatch,
        claude_sid,
        active_profile="feng-family",
        cli_meta=row,
        messages=[],  # Claude Code transcripts are global, not state.db backed
    )
    assert cap.get("error") is None
    assert cap["status"] == 200
    assert cap["data"]["session"]["session_id"] == claude_sid
    assert calls["claim"] == 1


def test_root_profile_detail_load_behaviour_is_unchanged(monkeypatch):
    # Under the root profile the gate already passed for a profile-less row,
    # so the request reaches the claim path exactly as before — the ownership
    # fallback never even runs.
    cap, calls = _detail_load(
        monkeypatch,
        OLD_SID,
        active_profile="default",
        cli_meta={},
        messages=[],
    )
    assert calls["claim"] == 1
    assert cap["status"] == 200

    # A genuinely missing session still 404s via the claim path's own refusal.
    cap, calls = _detail_load(
        monkeypatch,
        OLD_SID,
        active_profile="default",
        cli_meta={},
        messages=[],
        claim=(None, "no_foreign_state"),
    )
    assert calls["claim"] == 1
    assert cap["status"] == 404
    assert cap["error"] == "Session not found"


@pytest.mark.parametrize("surface", ["detail", "share"])
def test_older_cli_session_decodes_transcript_once(surface, monkeypatch):
    reads = []
    transcript = _transcript()

    def read_messages(sid, **_kwargs):
        reads.append(sid)
        return transcript

    cap = _capture(monkeypatch)
    handler = MagicMock()
    monkeypatch.setattr(routes, "get_session", MagicMock(side_effect=KeyError(OLD_SID)))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "test-profile")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "get_cli_session_messages", read_messages)
    monkeypatch.setattr(routes, "_session_index_marks_was_webui", lambda _sid: False)
    monkeypatch.setattr(routes, "_session_deleted_tombstone_marks_was_webui", lambda _sid: False)

    if surface == "share":
        snap, _, _ = routes._resolve_share_session_pair(OLD_SID, handler)
        assert snap.messages == transcript
    else:
        assert routes.handle_get(handler, urlparse(f"/api/session?session_id={OLD_SID}&messages=0&resolve_model=0"))
        assert cap["status"] == 200
    assert reads == [OLD_SID]
