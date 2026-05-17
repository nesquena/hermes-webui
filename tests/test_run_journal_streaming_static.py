from pathlib import Path


def test_streaming_initializes_one_run_journal_writer_per_stream():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    register_idx = src.index("register_active_run(")
    writer_idx = src.index("RunJournalWriter(session_id, stream_id)", register_idx)
    cancel_idx = src.index("cancel_event = threading.Event()", writer_idx)

    assert "from api.run_journal import RunJournalWriter" in src
    assert register_idx < writer_idx < cancel_idx


def test_streaming_journals_sse_events_before_queue_delivery():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    put_idx = src.index("def put(event, data):")
    journal_idx = src.index("run_journal.append_sse_event(event, data)", put_idx)
    # Stage-364 maintainer fix: put() now pushes 3-tuples (event, data, event_id)
    # so the SSE consumer can emit `id:` on live frames. Accept either shape
    # so this test survives both the v0.51.71 in-flight fix and a future revert.
    try:
        queue_idx = src.index("q.put_nowait((event, data, event_id))", put_idx)
    except ValueError:
        queue_idx = src.index("q.put_nowait((event, data))", put_idx)
    block = src[put_idx:queue_idx]

    assert put_idx < journal_idx < queue_idx
    assert "Failed to append run journal event" in block
