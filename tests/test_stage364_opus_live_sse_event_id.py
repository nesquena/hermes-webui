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
    `RunJournalWriter.append_sse_event()` return and writes it to
    `STREAM_LAST_EVENT_ID[stream_id]`.
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
    assert "journaled = run_journal.append_sse_event(event, data)" in put_body, (
        "put() must capture append_sse_event return value"
    )
    assert "STREAM_LAST_EVENT_ID[stream_id]" in put_body, (
        "put() must write event_id to STREAM_LAST_EVENT_ID[stream_id] — "
        "this is the side-channel the SSE consumer reads at emit time"
    )


def test_stream_channel_queue_item_carries_per_event_id_with_legacy_fallback():
    """StreamChannel queue items need per-frame ids; legacy queues stay 2-tuples."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 2500]
    assert 'queue_item = (event, data, event_id) if event_id and hasattr(q, "subscribe_with_snapshot") else (event, data)' in put_body, (
        "StreamChannel events must carry their own event_id while legacy queue "
        "consumers retain the 2-tuple shape"
    )
    assert "q.put_nowait(queue_item)" in put_body


def test_gateway_queue_item_carries_per_event_id_with_legacy_fallback():
    """Gateway-backed WebUI chat must preserve the same live cursor invariant."""
    put_def_idx = GATEWAY_CHAT_PY.find("def put_gateway_event(event, data):")
    assert put_def_idx != -1, "put_gateway_event(event, data) not found"
    put_body = GATEWAY_CHAT_PY[put_def_idx:put_def_idx + 1800]
    assert 'queue_item = (event, data, event_id) if event_id and hasattr(q, "subscribe_with_snapshot") else (event, data)' in put_body, (
        "Gateway live events must carry their own event_id for StreamChannel "
        "subscribers while preserving legacy queue compatibility"
    )
    assert "q.put_nowait(queue_item)" in put_body


def test_sse_handler_reads_event_id_from_side_channel():
    """The SSE consumer in _handle_sse_stream must read STREAM_LAST_EVENT_ID
    and pass it to _sse_with_id when present."""
    handler_idx = ROUTES_PY.find("def _handle_sse_stream(handler, parsed):")
    assert handler_idx != -1, "_handle_sse_stream not found"
    handler_body = ROUTES_PY[handler_idx:handler_idx + 4000]
    assert "STREAM_LAST_EVENT_ID.get(stream_id)" in handler_body, (
        "_handle_sse_stream must read STREAM_LAST_EVENT_ID[stream_id] to "
        "get the event_id for emit"
    )
    assert "_sse_with_id(handler, event, data, event_id)" in handler_body, (
        "_handle_sse_stream must call _sse_with_id when event_id is set"
    )


def test_cleanup_pops_stream_last_event_id():
    """The streaming worker's finally block must pop STREAM_LAST_EVENT_ID
    alongside the other STREAM_* dicts to prevent memory leak."""
    # Find the cleanup block — multiple .pop(stream_id, None) lines
    cleanup_idx = STREAMING_PY.find("STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)")
    assert cleanup_idx != -1, "cleanup block not found"
    cleanup_block = STREAMING_PY[cleanup_idx:cleanup_idx + 500]
    assert "STREAM_LAST_EVENT_ID.pop(stream_id, None)" in cleanup_block, (
        "STREAM_LAST_EVENT_ID must be popped on worker finally to prevent "
        "unbounded memory growth across streams"
    )


def test_imports_present():
    """STREAM_LAST_EVENT_ID must be imported in both streaming.py (writer)
    and routes.py (reader)."""
    assert "STREAM_LAST_EVENT_ID," in STREAMING_PY, "streaming.py must import"
    assert "STREAM_LAST_EVENT_ID," in ROUTES_PY, "routes.py must import"
