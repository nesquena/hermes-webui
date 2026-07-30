"""Regression tests for #3346: merge_session_messages_append_only fails to
deduplicate legacy state messages (messages without explicit id/message_id).

Three repro rows from the bug report:

  Row A — Gap 1: empty sidecar, duplicate state (no timestamps)
  Row B — Gap 2: non-empty sidecar, duplicate legacy state
  Row C — Gap 2 (replay variant): sidecar has explicit id, state has
          two copies of the same legacy message; first is consumed by
          the replay-prefix branch, second must still be deduped.
"""
from __future__ import annotations

import pytest

from api.models import merge_session_messages_append_only


def _legacy(role: str, content: str, timestamp=None) -> dict:
    msg = {"role": role, "content": content}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _identified(role: str, content: str, msg_id: str, timestamp=None) -> dict:
    msg = {"id": msg_id, "role": role, "content": content}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


# ── Row A: Gap 1 — empty sidecar, early-return path ──────────────────────────

@pytest.mark.parametrize("use_watermark", [False, True])
def test_empty_sidecar_deduplicates_identical_legacy_state(use_watermark):
    """Empty sidecar with duplicate state rows must return a single message."""
    a = _legacy("user", "hello")
    state = [a, a]  # true duplicate — same role, content, no timestamp
    watermark = "2030-01-01T00:00:00Z" if use_watermark else None
    result = merge_session_messages_append_only([], state, truncation_watermark=watermark)
    assert len(result) == 1, f"expected 1 (deduped), got {len(result)}"
    assert result[0] == a


# ── Row B: Gap 2 — non-empty sidecar, legacy dup in state ────────────────────

def test_nonempty_sidecar_deduplicates_identical_legacy_state():
    """Non-empty sidecar: duplicate legacy state rows must not both appear."""
    sidecar = [_identified("system", "sys", msg_id="s1")]
    a = _legacy("user", "hello")
    state = [a, a]
    result = merge_session_messages_append_only(sidecar, state)
    contents = [m["content"] for m in result]
    assert contents == ["sys", "hello"], f"expected [sys, hello], got {contents}"


# ── Row C: Gap 2 (replay) — sidecar id-keyed, state has two legacy copies ────

def test_replay_then_legacy_dup_is_deduped():
    """Sidecar has {id, a}; state has [a, a].
    First state 'a' is consumed by the replay-prefix branch; second must be
    caught by the dedup guard rather than appended.
    """
    sidecar = [_identified("user", "hello", msg_id="a")]
    a = _legacy("user", "hello")
    state = [a, a]
    result = merge_session_messages_append_only(sidecar, state)
    assert len(result) == 1, f"expected 1, got {len(result)}: {result}"
    assert result[0]["id"] == "a", "sidecar message should win"


# ── Same-second distinct turns must be preserved ─────────────────────────────

def test_same_second_distinct_turns_preserved():
    """Two messages with the same role+content but different sub-second
    timestamps are legitimately distinct turns and must not be collapsed.
    """
    sidecar = [_legacy("user", "start", timestamp=1779300508)]
    state = [
        _legacy("assistant", "Still working", timestamp=1779300509.12663),
        _legacy("assistant", "Still working", timestamp=1779300509.82718),
    ]
    result = merge_session_messages_append_only(sidecar, state)
    assert len(result) == 3, f"expected 3 (distinct sub-second turns), got {len(result)}"
