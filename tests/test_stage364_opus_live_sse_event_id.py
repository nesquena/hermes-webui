"""Regression test for stage-364 Opus-caught SHOULD-FIX (per-frame cursor):

When the live SSE stream errors mid-stream and the frontend falls back to
journal replay, live frames must carry an `id:` field so the frontend's
`_lastRunJournalSeq` cursor advances during the live phase. Otherwise replay
arrives with `after_seq=0` and the server replays every journaled event from
seq 1, double-rendering tokens against the live-phase `assistantText`
accumulator.

Implementation:

  - api/config.py adds `STREAM_LAST_EVENT_ID: dict = {}` module-level dict.
  - api/streaming.py `put()` captures `journaled["event_id"]` from
    `RunJournalWriter.append_and_publish_sse_event()` and writes it to
    `STREAM_LAST_EVENT_ID[stream_id]` only after queue publication succeeds.
  - StreamChannel queue items carry `(event, data, event_id)` so active
    subscribers emit each frame with its own id instead of the latest global id.
  - Legacy plain queues keep `(event, data)` and use `STREAM_LAST_EVENT_ID` as a
    compatibility fallback.
  - api/streaming.py finally-block cleanup pops STREAM_LAST_EVENT_ID.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
GATEWAY_CHAT_PY = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")


def test_stream_last_event_id_dict_exists_in_config():
    """`STREAM_LAST_EVENT_ID` must be declared as a module-level dict in
    api/config.py alongside the other STREAM_* registries."""
    assert "STREAM_LAST_EVENT_ID: dict = {}" in CONFIG_PY, (
        "STREAM_LAST_EVENT_ID dict missing from api/config.py — needed as "
        "the side-channel that lets SSE consumers emit `id:` on live frames"
    )


def test_put_writes_event_id_to_side_channel_dict():
    """The `put()` helper must capture the event_id from the journal and
    write it to STREAM_LAST_EVENT_ID[stream_id]."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    assert put_def_idx != -1, "put(event, data) not found in api/streaming.py"
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 2500]
    assert "run_journal.append_and_publish_sse_event(event, data, _publish_journaled)" in put_body, (
        "put() must capture the canonical journal event inside the append+publish transaction"
    )
    assert "STREAM_LAST_EVENT_ID[stream_id]" in put_body, (
        "put() must write event_id to STREAM_LAST_EVENT_ID[stream_id] — "
        "this is the side-channel the SSE consumer reads at emit time"
    )


def test_stream_channel_queue_item_carries_per_event_id_with_legacy_fallback():
    """StreamChannel queue items need per-frame ids; legacy queues stay 2-tuples."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 3000]
    assert 'queue_item = (event, data, None) if hasattr(q, "subscribe_with_snapshot") else (event, data)' in put_body, (
        "StreamChannel events must keep the 3-tuple shape for snapshot-capable "
        "queues while legacy queue consumers retain the 2-tuple shape "
        "(upstream #7272 metering events carry no event id)"
    )
    assert "q.put_nowait(queue_item)" in put_body


def test_gateway_queue_item_carries_per_event_id_with_legacy_fallback():
    """Gateway-backed WebUI chat must preserve the same live cursor invariant."""
    put_def_idx = GATEWAY_CHAT_PY.find("def put_gateway_event(event, data):")
    assert put_def_idx != -1, "put_gateway_event(event, data) not found"
    put_body = GATEWAY_CHAT_PY[put_def_idx:put_def_idx + 1800]
    assert 'queue_item = (event, data, None) if hasattr(q, "subscribe_with_snapshot") else (event, data)' in put_body, (
        "Gateway live events must keep the 3-tuple shape for StreamChannel "
        "subscribers while preserving legacy queue compatibility "
        "(upstream #7272 metering events carry no event id)"
    )
    assert "q.put_nowait(queue_item)" in put_body


def test_sse_handler_reads_event_id_from_side_channel():
    """The SSE consumer in _handle_sse_stream must read STREAM_LAST_EVENT_ID
    and pass it to _sse_with_id when present."""
    handler_idx = ROUTES_PY.find("def _handle_sse_stream(handler, parsed):")
    assert handler_idx != -1, "_handle_sse_stream not found"
    handler_body = ROUTES_PY[handler_idx:handler_idx + 5400]
    assert "STREAM_LAST_EVENT_ID.get(stream_id)" in handler_body, (
        "_handle_sse_stream must read STREAM_LAST_EVENT_ID[stream_id] to "
        "get the event_id for emit"
    )
    assert "_sse_with_id(handler, event, data, event_id)" in handler_body, (
        "_handle_sse_stream must call _sse_with_id when event_id is set"
    )


def test_stream_last_event_id_released_on_stream_teardown(tmp_path, monkeypatch):
    """STREAM_LAST_EVENT_ID must be released when the stream ends.

    Behavior-level (#7302 re-gate): the worker teardown delegates to
    ``release_stream_owned_registries``, so assert the registry row is gone
    after the real worker exit (cancellation before admission -> ``q is None``)
    instead of grepping ``api/streaming.py`` for the inline pop.
    """
    import threading

    from api import config
    import api.streaming as streaming

    session_id = "sess_364_cursor_teardown"
    stream_id = "stream-364-cursor"
    config.register_stream_owner(stream_id, session_id)
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    config.STREAM_LAST_EVENT_ID[stream_id] = "ev-1"
    config.STREAMS.pop(stream_id, None)

    streaming._run_agent_streaming(session_id, "hello", "test-model", None, stream_id)

    assert stream_id not in config.STREAM_LAST_EVENT_ID, (
        "STREAM_LAST_EVENT_ID must be released on stream teardown to prevent "
        "unbounded memory growth across streams"
    )


def test_stream_owned_registries_covers_every_stream_registry():
    """The canonical release helper must own the complete registry set.

    Contract check on the helper itself (not on source text): every per-stream
    registry the lifecycle writes must be covered by
    ``stream_owned_registries()``, otherwise a teardown path driven by that list
    silently strands the missing one -- exactly the residual-owner bug class
    this PR closes.
    """
    from api import config

    covered = {id(registry) for registry in config.stream_owned_registries()}
    for registry, name in [
        (config.STREAMS, "STREAMS"),
        (config.AGENT_INSTANCES, "AGENT_INSTANCES"),
        (config.CANCEL_FLAGS, "CANCEL_FLAGS"),
        (config.STREAM_GOAL_RELATED, "STREAM_GOAL_RELATED"),
        (config.STREAM_PARTIAL_TEXT, "STREAM_PARTIAL_TEXT"),
        (config.STREAM_REASONING_TEXT, "STREAM_REASONING_TEXT"),
        (config.STREAM_LIVE_TOOL_CALLS, "STREAM_LIVE_TOOL_CALLS"),
        (config.STREAM_LAST_EVENT_ID, "STREAM_LAST_EVENT_ID"),
    ]:
        assert id(registry) in covered, (
            f"stream_owned_registries() must cover {name} so every teardown "
            "path releases it"
        )


def test_imports_present():
    """STREAM_LAST_EVENT_ID must be imported in both streaming.py (writer)
    and routes.py (reader)."""
    assert "STREAM_LAST_EVENT_ID," in STREAMING_PY, "streaming.py must import"
    assert "STREAM_LAST_EVENT_ID," in ROUTES_PY, "routes.py must import"
