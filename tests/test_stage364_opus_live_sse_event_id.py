"""Regression test for live SSE event ids:

When the live SSE stream errors mid-stream and the frontend falls back to
journal replay, live frames must carry an `id:` field so the frontend's
`_lastRunJournalSeq` cursor advances during the live phase. Otherwise replay
arrives with `after_seq=0` and the server replays every journaled event from
seq 1, double-rendering tokens against the live-phase `assistantText`
accumulator.

Implementation:

  - api/streaming.py `put()` captures `journaled["event_id"]` from
    `RunJournalWriter.append_sse_event()`.
  - The queue item carries that event id with the event payload, so replay
    cursors track the event that was actually emitted rather than a side-channel
    "latest" value that can mis-stamp buffered backlog events.
  - api/routes.py `_handle_sse_stream` unpacks the per-item id and uses
    `_sse_with_id` when set.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")


def test_stream_channel_tracks_per_item_event_ids():
    assert "subscribe_with_snapshot" in CONFIG_PY
    assert "_event_id_from_item" in CONFIG_PY
    assert "_last_event_id" in CONFIG_PY


def test_put_writes_event_id_into_queue_item():
    """The `put()` helper must capture the event_id from the journal and
    write it to STREAM_LAST_EVENT_ID[stream_id]."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    assert put_def_idx != -1, "put(event, data) not found in api/streaming.py"
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 2500]
    assert "journaled = run_journal.append_sse_event(event, data)" in put_body, (
        "put() must capture append_sse_event return value"
    )
    assert "q.put_nowait((event, data, event_id))" in put_body, (
        "put() must queue event_id with its own SSE event so buffered backlog "
        "frames cannot inherit a later event's id"
    )


def test_queue_tuple_shape_is_three_tuple_for_journaled_events():
    """Journaled live events must carry their own event id in the queue item."""
    put_def_idx = STREAMING_PY.find("def put(event, data):")
    put_body = STREAMING_PY[put_def_idx:put_def_idx + 2500]
    assert "q.put_nowait((event, data, event_id))" in put_body


def test_sse_handler_reads_event_id_from_queue_item():
    """The SSE consumer must use the event id attached to the queue item."""
    handler_idx = ROUTES_PY.find("def _handle_sse_stream(handler, parsed):")
    assert handler_idx != -1, "_handle_sse_stream not found"
    handler_body = ROUTES_PY[handler_idx:handler_idx + 4000]
    assert "_stream_queue_item_parts(item)" in handler_body
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
    """STREAM_LAST_EVENT_ID remains available for cleanup/backcompat."""
    assert "STREAM_LAST_EVENT_ID," in STREAMING_PY, "streaming.py must import"
