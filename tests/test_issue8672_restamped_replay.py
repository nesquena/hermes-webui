"""Regression test for PR #8672 gate finding F1 (2026-11-20).

A later user row disappears after a restamped replay.

Saved transcript: user Q at 1000, assistant A at 1001, with stable ids minted
by the real _assign_stable_message_ids() and stamped by the real
_stamp_missing_message_timestamps(). The real SQLite store holds the same pair
restamped to 2000/2001, followed by a third durable row: user Q with a SQL
NULL timestamp.

The positional match in api/models.py suppresses the restamped mirror but
charges the saved occurrence's replay capacity only when a second,
clock-sensitive search also matches. With different clocks that search misses,
so the saved Q still has capacity, and the NULL-clock row consumes it. One
saved occurrence absorbs two durable rows, contrary to the at-most-one-replay
invariant this PR establishes elsewhere.

Fix: charge replay capacity for the positional target in the same decision
that suppresses it, so a saved occurrence absorbs at most one durable replay.
"""

from api.models import merge_session_messages_append_only
from api.streaming import (
    _assign_stable_message_ids,
    _stamp_missing_message_timestamps,
)


def _make_saved_transcript():
    """Saved transcript with stable ids minted by the real producer."""
    rows = [
        {"role": "user", "content": "Q", "timestamp": 1000.0},
        {"role": "assistant", "content": "A", "timestamp": 1001.0},
    ]
    _assign_stable_message_ids(rows)
    _stamp_missing_message_timestamps(rows, now=9_000_000.0)
    return rows


def _make_state_db_restamped():
    """State.db holds restamped mirror (2000/2001) plus NULL-clock Q."""
    return [
        {
            "role": "user",
            "content": "Q",
            "timestamp": 2000.0,
            "_state_db_row_id": 3,
        },
        {
            "role": "assistant",
            "content": "A",
            "timestamp": 2001.0,
            "_state_db_row_id": 4,
        },
        {
            "role": "user",
            "content": "Q",
            "timestamp": None,
            "_state_db_row_id": 5,
        },
    ]


def _make_state_db_matching_clock():
    """Control: state.db holds matching-clock mirror (1000/1001) plus NULL-clock Q."""
    return [
        {
            "role": "user",
            "content": "Q",
            "timestamp": 1000.0,
            "_state_db_row_id": 3,
        },
        {
            "role": "assistant",
            "content": "A",
            "timestamp": 1001.0,
            "_state_db_row_id": 4,
        },
        {
            "role": "user",
            "content": "Q",
            "timestamp": None,
            "_state_db_row_id": 5,
        },
    ]


def test_restamped_mirror_plus_null_clock_row_keeps_later_user_row():
    """Gate finding F1: restamped mirror must not absorb the NULL-clock row."""
    sidecar = _make_saved_transcript()
    state = _make_state_db_restamped()

    merged = merge_session_messages_append_only(sidecar, state)

    # Expected: Q, A, Q -- the NULL-clock row must survive.
    user_rows = [m for m in merged if m.get("role") == "user" and m.get("content") == "Q"]
    assert len(user_rows) == 2, (
        "the later NULL-clock user row was suppressed: "
        f"expected Q,A,Q got {[m.get('content') for m in merged]}"
    )


def _make_state_db_restamped_only():
    """State.db holds only the restamped mirror (2000/2001), no NULL-clock row."""
    return [
        {
            "role": "user",
            "content": "Q",
            "timestamp": 2000.0,
            "_state_db_row_id": 3,
        },
        {
            "role": "assistant",
            "content": "A",
            "timestamp": 2001.0,
            "_state_db_row_id": 4,
        },
    ]


def test_restamped_mirror_alone_collapses_to_saved_pair():
    """Bare restamped mirror suppresses onto the saved pair, exactly once.

    Scenario ① of the gate finding without the later NULL-clock row: the
    clock-tolerant positional match is the only consumer, so the restamped
    mirror must collapse onto the saved Q,A and leave exactly two rows --
    no duplicated prompt and no dropped sidecar row.  Pins the base
    suppression semantics the capacity-charging fix builds on.
    """
    sidecar = _make_saved_transcript()
    state = _make_state_db_restamped_only()

    merged = merge_session_messages_append_only(sidecar, state)

    contents = [m.get("content") for m in merged]
    assert contents == ["Q", "A"], (
        "bare restamped mirror must collapse onto the saved pair exactly once: "
        f"expected ['Q', 'A'] got {contents}"
    )


def test_matching_clock_mirror_plus_null_clock_row_keeps_later_user_row():
    """Control: matching-clock mirror also keeps the NULL-clock row."""
    sidecar = _make_saved_transcript()
    state = _make_state_db_matching_clock()

    merged = merge_session_messages_append_only(sidecar, state)

    user_rows = [m for m in merged if m.get("role") == "user" and m.get("content") == "Q"]
    assert len(user_rows) == 2, (
        "control failed: matching-clock mirror should also keep NULL-clock row: "
        f"expected Q,A,Q got {[m.get('content') for m in merged]}"
    )
