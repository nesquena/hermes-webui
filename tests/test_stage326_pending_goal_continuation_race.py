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


def test_stream_teardown_preserves_pending_goal_continuation(tmp_path, monkeypatch):
    """REGRESSION GUARD (stage-326): no stream teardown path may discard
    ``PENDING_GOAL_CONTINUATION``.

    Behavior-level (#7302 re-gate): the marker is consumed atomically by the
    routes.py consumer, so discarding it in stream teardown races against the
    frontend's SSE-receive -> POST /chat/start round trip and erases the marker
    before it can be read. Exercised through the real worker exit path
    (cancellation before admission -> ``q is None``) and through the canonical
    release helper, which is the single teardown entry point.
    """
    import threading

    from api import config
    import api.streaming as streaming

    session_id = "sess_326_pending_survives"
    stream_id = "stream-326-pending"
    config.register_stream_owner(stream_id, session_id)
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    config.STREAM_GOAL_RELATED[stream_id] = True
    config.PENDING_GOAL_CONTINUATION.add(session_id)
    config.STREAMS.pop(stream_id, None)
    try:
        streaming._run_agent_streaming(
            session_id, "hello", "test-model", None, stream_id
        )
        assert session_id in config.PENDING_GOAL_CONTINUATION, (
            "REGRESSION: the stream teardown discarded "
            "PENDING_GOAL_CONTINUATION. This races against the consumer in "
            "routes.py and breaks the goal-continuation chain; the discard must "
            "live ONLY in routes.py's `_start_chat_stream_for_session` "
            "consumer path."
        )
        assert stream_id not in config.STREAM_GOAL_RELATED, (
            "the stream-owned rows must still be released on that same path"
        )
    finally:
        config.PENDING_GOAL_CONTINUATION.discard(session_id)

    # The canonical release helper is equally scoped: stream-owned rows only.
    config.register_stream_owner(stream_id, session_id)
    config.STREAM_GOAL_RELATED[stream_id] = True
    config.PENDING_GOAL_CONTINUATION.add(session_id)
    try:
        config.release_stream_owned_registries(stream_id, session_id=session_id)
        assert stream_id not in config.STREAM_GOAL_RELATED, (
            "release_stream_owned_registries must release the stream's rows"
        )
        assert session_id in config.PENDING_GOAL_CONTINUATION, (
            "release_stream_owned_registries must not touch the session's "
            "PENDING_GOAL_CONTINUATION marker"
        )
    finally:
        config.PENDING_GOAL_CONTINUATION.discard(session_id)


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


def test_stream_goal_related_release_is_keyed_by_stream_id():
    """Releasing the ending stream must not erase another stream's goal flag.

    Behavior-level (#7302 re-gate): the teardown is keyed by the ending
    ``stream_id``. A session can have overlapping streams over its lifetime, so
    a session-keyed release would drop the classification of a stream that is
    still live (and, on the pre-start path, one that is about to be admitted).
    """
    from api import config

    session_id = "sess_326_keyed_by_stream"
    ending_stream = "stream-326-ending"
    other_stream = "stream-326-other"
    config.register_stream_owner(ending_stream, session_id)
    config.register_stream_owner(other_stream, session_id)
    config.STREAM_GOAL_RELATED[ending_stream] = True
    config.STREAM_GOAL_RELATED[other_stream] = True

    try:
        config.release_stream_owned_registries(ending_stream, session_id=session_id)

        assert ending_stream not in config.STREAM_GOAL_RELATED, (
            "the ending stream's classification must be released"
        )
        assert config.STREAM_GOAL_RELATED.get(other_stream) is True, (
            "releasing one stream must leave the other stream's goal classification "
            "on the same session intact"
        )
        assert other_stream in config.STREAM_SESSION_OWNERS, (
            "releasing one stream must not unregister another stream's owner"
        )
    finally:
        # Greptile on #8108: release both streams so no shared registry entry leaks.
        for sid in (ending_stream, other_stream):
            config.release_stream_owned_registries(sid, session_id=session_id)
            config.STREAM_GOAL_RELATED.pop(sid, None)


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
