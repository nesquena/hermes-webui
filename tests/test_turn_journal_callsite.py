from pathlib import Path


def test_chat_start_appends_submitted_turn_journal_before_worker_thread_start():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    save_idx = src.index("_prepare_chat_start_session_for_stream(")
    append_idx = src.index("append_turn_journal_event(", save_idx)
    thread_idx = src.index("threading.Thread(", append_idx)

    assert save_idx < append_idx < thread_idx
    assert '"event": "submitted"' in src[append_idx:thread_idx]
    assert '"role": "user"' in src[append_idx:thread_idx]


def test_chat_start_writes_turn_journal_after_session_lock_and_handles_failure():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    lock_idx = src.index("with session_lock:")
    append_idx = src.index("append_turn_journal_event(", lock_idx)
    stream_registration_idx = src.index("STREAMS[stream_id] = stream", append_idx)
    lock_block = src[lock_idx:append_idx]
    append_block = src[append_idx:stream_registration_idx]

    assert "append_turn_journal_event(" not in lock_block
    assert "except Exception:" in append_block
    assert "Failed to append submitted turn journal event" in append_block


def test_pending_decision_resolution_is_durable_before_worker_start():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    append_idx = src.index("append_turn_journal_event(", src.index("def _start_chat_stream_for_session"))
    resolve_idx = src.index("mark_pending_decision_resolved(", append_idx)
    thread_idx = src.index("threading.Thread(", resolve_idx)
    assert append_idx < resolve_idx < thread_idx
    assert '"relation": "resolve_decision"' in src[append_idx:resolve_idx]
