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
    """The routes.py consumer must consume the marker in one atomic step, so
    the marker is single-use (one continuation = one auto-flag).

    #7862 moved the check+discard into
    ``api.goal_continuation_store.consume_pending_goal_continuation``, which
    performs the match, the ``goal_related`` decision input, and the discard
    under ONE module-level RLock. That is strictly tighter than the previous
    "check, set, discard" sequence, so the invariant this guard protects is
    unchanged -- only its shape moved.
    """
    src = _read_routes()

    # Find the consumption check.
    m = re.search(
        r"if not goal_related and s\.session_id in PENDING_GOAL_CONTINUATION:.*?"
        r"consume_pending_goal_continuation\(\s*"
        r"s\.session_id,\s*msg,\s*goal_continuation_id\s*"
        r",\s*goal_continuation_attempt_id\s*,?\s*\)",
        src,
        re.DOTALL,
    )
    assert m is not None, (
        "routes.py must consume PENDING_GOAL_CONTINUATION atomically via "
        "consume_pending_goal_continuation (check + set goal_related + "
        "discard in the same block)"
    )
    # The consume must be within ~10 lines of the check (atomic block).
    block = m.group(0)
    line_count = block.count("\n")
    assert line_count <= 10, (
        f"PENDING_GOAL_CONTINUATION check + consume span {line_count} lines; "
        "should be tight atomic block"
    )
    # The match-gated store is the ONLY place that discards for a chat start.
    assert "PENDING_GOAL_CONTINUATION.discard(s.session_id)" not in src


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
    # Must be within ~900 chars (close to the add). The window also covers the
    # #7862 round-3 token mint between the marker add and the SSE emission.
    assert event_idx < 900, (
        "PENDING_GOAL_CONTINUATION.add must immediately precede the "
        "goal_continue SSE emission"
    )
