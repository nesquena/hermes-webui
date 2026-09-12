from pathlib import Path


def test_chat_start_appends_submitted_turn_journal_before_worker_thread_start():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    admission_idx = src.index("def _commit_chat_start_admission(")
    save_idx = src.index("prepared = prepare(", admission_idx)
    append_idx = src.index("append_turn_journal_event(", save_idx)
    thread_idx = src.index("threading.Thread(", append_idx)

    assert save_idx < append_idx < thread_idx
    assert '"event": "submitted"' in src[append_idx:thread_idx]
    assert '"role": "user"' in src[append_idx:thread_idx]


def test_chat_start_writes_turn_journal_after_session_lock_and_handles_failure():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    start_idx = src.index("def _start_chat_stream_for_session(")
    lock_idx = src.index("with session_lock:", start_idx)
    delegate_idx = src.index("return _commit_chat_start_admission(", lock_idx)
    admission_idx = src.index("def _commit_chat_start_admission(")
    append_idx = src.index("append_turn_journal_event(", admission_idx)
    stream_registration_idx = src.index("STREAMS[stream_id] = stream", append_idx)
    append_block = src[append_idx:stream_registration_idx]

    assert admission_idx < append_idx < stream_registration_idx
    assert lock_idx < delegate_idx
    assert "except Exception:" in append_block
    assert "Failed to append submitted turn journal event" in append_block
