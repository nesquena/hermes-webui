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
    marker). Round 2 moved the record (marker + prompt + expiry) behind
    ``api.goals`` so streaming/gateway/routes share one owner; the
    stage-326 atomicity contract now lives there: check + drop happen
    under one lock-held block, and routes.py must not discard anywhere
    else.
    """
    src = _read_routes()

    # 1. The admission block routes through the helper.
    # #7855: admission is by continuation ID, not message text.
    # #7855 rebase compat (#7249): admission runs INSIDE the session lock, in
    # consume_continuation_markers() — never up front — so a rejected start
    # rolls back the whole admission (see the receipt guards below).
    m = re.search(
        r"def consume_continuation_markers\(\).*?"
        r"if not goal_related and goal_continuation_id:\s*\n\s*"
        r"receipt = _consume_pending_goal_continuation\(",
        src,
        re.S,
    )
    assert m is not None, (
        "routes.py must consume PENDING_GOAL_CONTINUATION via "
        "_consume_pending_goal_continuation(gated on goal_continuation_id), "
        "inside consume_continuation_markers()"
    )

    # 2. routes.py delegates to api.goals (single record owner).
    assert "from api.goals import consume_pending_goal_continuation" in src, (
        "routes.py helper must delegate to api.goals."
        "consume_pending_goal_continuation"
    )
    # 2b. #7855 rollback receipt: admission pops the record as well as the
    #     marker, so a rejected start must restore both halves through
    #     api.goals — re-adding the marker alone left the retry unmatchable.
    assert "from api.goals import restore_pending_goal_continuation" in src, (
        "restore_consumed_continuation_markers() must delegate to "
        "api.goals.restore_pending_goal_continuation so the record half "
        "is restored too (marker-only rollback is stale after #7855)"
    )

    # 3. No stray direct discard anywhere in routes.py: the drop is owned
    #    by api.goals (lock-held, check + drop in one block). The one allowed
    #    exception is master's `consume_continuation_markers()` closure, which
    #    consumes the legacy #1932 SET marker (a different object from the
    #    id-keyed record api.goals owns) for the plain no-id path.
    direct = re.findall(r"PENDING_GOAL_CONTINUATION\.discard", src)
    allowed = re.findall(r"def consume_continuation_markers\(\)[^}]*?PENDING_GOAL_CONTINUATION\.discard", src, re.S)
    assert len(direct) == len(allowed), (
        f"PENDING_GOAL_CONTINUATION.discard must not appear in routes.py "
        f"(api/goals owns the record drop); found {len(direct)}, of which "
        f"{len(allowed)} sit inside the legacy consume_continuation_markers() closure"
    )


def test_goals_module_owns_atomic_check_and_drop():
    """stage-326 atomicity, relocated: api.goals holds check + drop of the
    record in ONE lock-held block (the two collections cannot drift, and a
    reader can never observe a half-consumed record)."""
    goals_src = Path(__file__).parents[1].joinpath("api", "goals.py").read_text(encoding="utf-8")
    drop = re.search(
        r"def _drop_pending_goal_continuation\([\s\S]*?"
        r"_cfg\.PENDING_GOAL_CONTINUATION\.discard[\s\S]*?"
        r"_cfg\.PENDING_GOAL_CONTINUATION_PROMPTS\.pop",
        goals_src,
    )
    assert drop is not None, (
        "api.goals must drop marker + prompt together in one helper "
        "(single record, no drift)"
    )
    block = drop.group(0)
    assert block.count("\n") <= 12, (
        "the atomic drop helper should stay tight (single record removal)"
    )
    # The consumer runs check + drop under the shared lock.
    consume = re.search(
        r"def consume_pending_goal_continuation\(.*?\n(?:.*\n)*?.*with _cfg\.PENDING_GOAL_CONTINUATION_LOCK:",
        goals_src,
    )
    assert consume is not None, (
        "api.goals.consume_pending_goal_continuation must hold the shared "
        "lock across check + drop"
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
    """Source-code ordering check: the continuation record must be
    registered BEFORE the goal_continue SSE event is put on the queue, so
    the marker is observable by the time the frontend reacts.

    #6885 round 2: the add runs through
    ``api.goals.register_pending_goal_continuation`` (marker + prompt +
    expiry as one record), and the SSE emission is gated on its return so a
    failed registration cannot leave the frontend queued without a server
    record to consume."""
    src = _read_streaming()
    m = re.search(
        r"register_pending_goal_continuation\(session_id, continuation_prompt\)",
        src,
    )
    assert m is not None, (
        "streaming.py must register the continuation record via "
        "register_pending_goal_continuation(session_id, continuation_prompt)"
    )
    add_idx = m.start()

    # Find the next goal_continue SSE event AFTER the registration.
    after_add = src[add_idx:]
    event_idx = after_add.find("put('goal_continue'")
    assert event_idx != -1, "no goal_continue emission after record registration"
    # Must be within ~500 chars (close to the registration).
    assert event_idx < 500, (
        "register_pending_goal_continuation must immediately precede the "
        "goal_continue SSE emission"
    )
    # The SSE event fires only when the registration succeeded.
    # #7855: registration returns the continuation ID (not a bool), so the
    # gate is now ``if not continuation_id``.
    assert "if not register_pending_goal_continuation(" in src or (
        "continuation_id = register_pending_goal_continuation(" in src
        and "if not continuation_id:" in src
    ), (
        "the goal_continue SSE event must be gated on the record "
        "registration return value"
    )
