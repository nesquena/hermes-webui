"""Regression tests for the legacy goal marker during server-owned delivery.

The durable goal-continuation registry is now authoritative.  The in-memory
``PENDING_GOAL_CONTINUATION`` set remains only for mixed-version browser tabs
and must still be consumed exactly once by whichever stream starts next.  A
server-owned stream is already explicitly goal-related, so marker consumption
is unconditional rather than gated by ``not goal_related``.
"""
import re
from pathlib import Path


def _read_streaming():
    return Path(__file__).parents[1].joinpath("api", "streaming.py").read_text(encoding="utf-8")


def _read_routes():
    return Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")


def test_streaming_finally_does_not_discard_pending_goal_continuation():
    """REGRESSION GUARD (stage-326): the streaming worker's `finally` block
    must NOT contain `PENDING_GOAL_CONTINUATION.discard(session_id)`.

    Doing so races against the frontend's SSE-receive → POST /chat/start
    round-trip and erases the marker before it can be consumed.
    """
    src = _read_streaming()

    # Find the cleanup block — STREAM_GOAL_RELATED.pop is a stable anchor.
    pop_idx = src.find("STREAM_GOAL_RELATED.pop(stream_id")
    assert pop_idx != -1, "STREAM_GOAL_RELATED cleanup not found — test needs update"

    # Look at the next ~600 chars (the immediate cleanup block).
    block = src[pop_idx:pop_idx + 600]

    # The discard must NOT appear in this cleanup block.
    assert "PENDING_GOAL_CONTINUATION.discard" not in block, (
        "REGRESSION: streaming.py's stream-cleanup block discards "
        "PENDING_GOAL_CONTINUATION. This races against the consumer in "
        "routes.py and breaks the goal-continuation chain. The discard "
        "must live ONLY in routes.py's `_start_chat_stream_for_session` "
        "consumer path."
    )


def test_routes_consumer_consults_durable_tombstone_without_parent_marker():
    """A delayed old-tab parent replay is rejected even when the marker moved to the child."""
    src = _read_routes()

    gate_idx = src.index("if not goal_related:")
    adopt_idx = src.index("elif legacy_goal_marker_consumed:", gate_idx)
    lock_idx = src.index("session_lock =", gate_idx)
    gate_block = src[gate_idx:lock_idx]
    assert "legacy_browser_goal_prompt_matches" in gate_block
    assert "s.session_id in PENDING_GOAL_CONTINUATION" not in gate_block
    assert "PENDING_GOAL_CONTINUATION.discard" not in gate_block

    server_branch = src[
        src.index('if source == "goal_continuation":', gate_idx):adopt_idx
    ]
    assert server_branch.index("bind_goal_continuation_stream") < server_branch.index(
        "PENDING_GOAL_CONTINUATION.discard"
    )

    adoption_block = src[adopt_idx:adopt_idx + 900]
    adopt_call = adoption_block.index("adopt_legacy_browser_goal_stream")
    discard_call = adoption_block.index("PENDING_GOAL_CONTINUATION.discard")
    prepare_call = adoption_block.index("_prepare_chat_start_session_for_stream")
    assert adopt_call < discard_call < prepare_call


def test_pending_goal_continuation_is_a_set():
    """The marker store must be a set so add/discard is GIL-safe single-op
    (mutated from streaming worker thread, read from HTTP threads)."""
    from api.config import PENDING_GOAL_CONTINUATION
    assert isinstance(PENDING_GOAL_CONTINUATION, set), (
        "PENDING_GOAL_CONTINUATION must be a set for thread-safe single-op "
        "add/discard semantics"
    )


def test_stream_goal_related_pop_keyed_by_stream_id():
    """STREAM_GOAL_RELATED.pop in the cleanup must be keyed by stream_id
    (the ending stream's id), not session_id — a different stream's flag
    must not be erased."""
    src = _read_streaming()
    # Search for the cleanup line.
    m = re.search(r"STREAM_GOAL_RELATED\.pop\(([^,)]+)", src)
    assert m is not None, "STREAM_GOAL_RELATED.pop not found in streaming.py"
    key = m.group(1).strip()
    assert key == "stream_id", (
        f"STREAM_GOAL_RELATED.pop must be keyed by stream_id, got {key!r}. "
        "Using session_id would erase a different stream's flag if two "
        "streams overlap on the same session."
    )


def test_goal_continue_set_marker_before_emitting_event():
    """Source-code ordering check: PENDING_GOAL_CONTINUATION.add must
    happen BEFORE the goal_continue SSE event is put on the queue, so the
    marker is observable by the time the frontend reacts."""
    src = _read_streaming()
    add_idx = src.find("PENDING_GOAL_CONTINUATION.add(session_id)")
    if add_idx == -1:
        # Tolerate slight phrasing variations.
        m = re.search(r"PENDING_GOAL_CONTINUATION\.add\([^)]*\)", src)
        assert m is not None, "PENDING_GOAL_CONTINUATION.add not found"
        add_idx = m.start()

    # Find the next goal_continue SSE event AFTER the add.
    after_add = src[add_idx:]
    event_idx = after_add.find("goal_continue")
    assert event_idx != -1, "no goal_continue emission after marker add"
    # Must be within ~500 chars (close to the add).
    assert event_idx < 500, (
        "PENDING_GOAL_CONTINUATION.add must immediately precede the "
        "goal_continue SSE emission"
    )
