"""Stage-326 integration test for #1951's PENDING_GOAL_CONTINUATION chain.

Opus advisor flagged a critical race during stage-326 review: the original
#1951 PR placed a `PENDING_GOAL_CONTINUATION.discard(session_id)` in the
streaming worker's `finally` block. Because `goal_continue` sets the marker
inside the SAME function call (line ~3328) that the `finally` then discards
it (line ~3553), the marker would be erased before the frontend could
receive the SSE event, post the next /chat/start, and trigger the
consumer-side `if session_id in PENDING_GOAL_CONTINUATION` check in
routes.py.

The fix removes the discard from streaming.py's finally and relies on the
consumer in routes.py to discard atomically when the marker is read.

These tests exercise the full chain to guard against the regression:
1. The streaming finally must NOT discard the marker
2. Setting the marker survives the streaming finally
3. routes.py consumer discards atomically on read
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


def test_routes_consumer_discards_atomically_on_read():
    """The routes.py consumer must discard the marker after consuming it,
    so the marker is single-use (one continuation = one auto-flag).

    #6885: consumption is delegated to the module-level helper
    ``_consume_pending_goal_continuation`` (admission correction: only a
    turn whose text matches the pending continuation prompt consumes the
    marker). The helper must keep check + discard in one tight atomic
    block, and the caller must not discard anywhere else.
    """
    src = _read_routes()

    # 1. The admission block routes through the helper with (session_id, msg).
    m = re.search(
        r"if not goal_related and _consume_pending_goal_continuation\(\s*s\.session_id,\s*msg\s*\):",
        src,
    )
    assert m is not None, (
        "routes.py must consume PENDING_GOAL_CONTINUATION via "
        "_consume_pending_goal_continuation(s.session_id, msg)"
    )

    # 2. Inside the helper: marker check + discard must stay in one tight
    #    atomic block (<= 10 lines), exactly the old stage-326 contract.
    helper = re.search(
        r"if session_id not in PENDING_GOAL_CONTINUATION:.*?PENDING_GOAL_CONTINUATION\.discard",
        src,
        re.DOTALL,
    )
    assert helper is not None, (
        "helper must keep check + discard in the same atomic block"
    )
    block = helper.group(0)
    line_count = block.count("\n")
    assert line_count <= 10, (
        f"PENDING_GOAL_CONTINUATION check + discard span {line_count} lines; "
        "should be tight atomic block"
    )

    # 3. No stray direct discard anywhere else in routes.py.
    direct = re.findall(r"PENDING_GOAL_CONTINUATION\.discard", src)
    assert len(direct) == 1, (
        f"PENDING_GOAL_CONTINUATION.discard must appear exactly once in routes.py "
        f"(inside the helper), found {len(direct)}"
    )


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
