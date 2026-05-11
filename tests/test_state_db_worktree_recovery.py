"""Regression for state.db × worktree-backed session recovery.

PR #2053 added worktree-backed session creation. PR #2041 added state.db
sidecar reconciliation. When a worktree-backed session's JSON sidecar is
lost (failed save, manual rm, restore-from-backup) and state.db is the only
source of truth, the recovery path must rebuild a sidecar that preserves
the worktree_* fields. Without that, the sidebar exempt-empty filter at
api/models.py:1067/1107 (which spares worktree-backed empty sessions) sees
no worktree_path on the rebuilt session and silently filters it out — the
session vanishes from the sidebar even though the worktree directory still
exists on disk.

Caught by Opus advisor on stage-337 review.
"""
from __future__ import annotations

from api.session_recovery import _state_db_row_to_sidecar


def test_state_db_recovery_preserves_worktree_metadata():
    """Recovered sidecar must keep worktree_path / worktree_branch / repo_root."""
    row = {
        "id": "abc123",
        "source": "webui",
        "title": "My worktree session",
        "model": "anthropic/claude-3-opus",
        "started_at": 1700000000,
        "parent_session_id": None,
        "message_count": 3,
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 1700000001},
            {"role": "assistant", "content": "hi", "timestamp": 1700000002},
            {"role": "user", "content": "more", "timestamp": 1700000003},
        ],
        "workspace": "/home/user/proj/.worktrees/hermes-1234",
        "worktree_path": "/home/user/proj/.worktrees/hermes-1234",
        "worktree_branch": "hermes/abc123",
        "worktree_repo_root": "/home/user/proj",
        "worktree_created_at": 1700000000,
    }

    sidecar = _state_db_row_to_sidecar(row)

    assert sidecar["session_id"] == "abc123"
    assert sidecar["title"] == "My worktree session"
    # The four worktree_* fields must survive the rebuild — without them the
    # sidebar filter at api/models.py:1067 hides the session.
    assert sidecar["worktree_path"] == "/home/user/proj/.worktrees/hermes-1234"
    assert sidecar["worktree_branch"] == "hermes/abc123"
    assert sidecar["worktree_repo_root"] == "/home/user/proj"
    assert sidecar["worktree_created_at"] == 1700000000
    # Workspace must round-trip from the row so terminal panels / file pickers
    # operate on the correct path, not on empty string.
    assert sidecar["workspace"] == "/home/user/proj/.worktrees/hermes-1234"
    # message_count must come from the row so the sidebar exempt-empty filter
    # accepts message-bearing sessions (was hard-coded 0 pre-fix).
    assert sidecar["message_count"] == 3


def test_state_db_recovery_non_worktree_session_unaffected():
    """A normal (non-worktree) session recovers exactly as before — None worktree fields."""
    row = {
        "id": "xyz789",
        "source": "webui",
        "title": "Normal chat",
        "model": "openai/gpt-4",
        "started_at": 1700000000,
        "parent_session_id": None,
        "message_count": 1,
        "messages": [{"role": "user", "content": "hello"}],
        # No workspace, no worktree_* fields on the row.
    }

    sidecar = _state_db_row_to_sidecar(row)

    assert sidecar["worktree_path"] is None
    assert sidecar["worktree_branch"] is None
    assert sidecar["worktree_repo_root"] is None
    assert sidecar["worktree_created_at"] is None
    assert sidecar["workspace"] == ""
    assert sidecar["message_count"] == 1


def test_state_db_recovery_zero_message_worktree_session_visible_in_sidebar():
    """An empty worktree-backed session recovered from state.db must NOT be
    silently filtered from the sidebar by the empty-session-exempt rule.

    Pre-fix: the recovery rebuilt a sidecar with no worktree_path → matched the
    empty-session filter → session disappeared from the sidebar even though
    the worktree directory still existed on disk. Now that worktree_path is
    propagated, the exemption clause at api/models.py:1070 fires.
    """
    row = {
        "id": "empty-worktree-abc",
        "source": "webui",
        "title": "Untitled",  # default before any user message
        "model": "anthropic/claude-3-opus",
        "started_at": 1700000000,
        "parent_session_id": None,
        "message_count": 0,
        "messages": [],
        "workspace": "/home/user/proj/.worktrees/hermes-empty",
        "worktree_path": "/home/user/proj/.worktrees/hermes-empty",
        "worktree_branch": "hermes/empty",
        "worktree_repo_root": "/home/user/proj",
        "worktree_created_at": 1700000000,
    }

    sidecar = _state_db_row_to_sidecar(row)

    # The compact() shape used in sidebar filtering is roughly the sidecar dict
    # with selected keys. The filter at api/models.py:1067 checks:
    #   title == 'Untitled' and message_count == 0 and not active_stream_id
    #   and not has_pending_user_message and not worktree_path
    # Pre-fix all 5 clauses matched → exempted FROM the result (i.e., hidden).
    # Post-fix the worktree_path clause is truthy, so the session SHOULD render.
    is_hidden_by_empty_filter = (
        sidecar.get("title", "Untitled") == "Untitled"
        and sidecar.get("message_count", 0) == 0
        and not sidecar.get("active_stream_id")
        and not sidecar.get("pending_user_message")
        and not sidecar.get("worktree_path")
    )
    assert not is_hidden_by_empty_filter, (
        "Worktree session was hidden by the empty-session exempt filter; "
        "worktree_path must be propagated through state.db recovery so the "
        "exempt clause in api/models.py:1070 does NOT match for this session."
    )
