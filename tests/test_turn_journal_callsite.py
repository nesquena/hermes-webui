import inspect

from api import routes


def test_chat_start_appends_submitted_turn_journal_before_worker_thread_start():
    src = inspect.getsource(routes._start_chat_stream_for_session)
    save_idx = src.index("_prepare_chat_start_session_for_stream(")
    append_idx = src.index("_append_prepared_chat_turn_journal(", save_idx)
    thread_idx = src.index("threading.Thread(", append_idx)
    append_src = inspect.getsource(routes._append_prepared_chat_turn_journal)

    assert save_idx < append_idx < thread_idx
    assert '"event": "submitted"' in append_src
    assert '"role": "user"' in append_src


def test_chat_start_writes_turn_journal_after_session_lock_and_handles_failure():
    src = inspect.getsource(routes._start_chat_stream_for_session)
    lock_idx = src.index("with session_lock:")
    append_idx = src.index("_append_prepared_chat_turn_journal(", lock_idx)
    stream_registration_idx = src.index("STREAMS[stream_id] = stream", append_idx)
    lock_block = src[lock_idx:append_idx]
    append_block = src[append_idx:stream_registration_idx]
    helper_src = inspect.getsource(routes._append_prepared_chat_turn_journal)

    assert "_append_prepared_chat_turn_journal(" not in lock_block
    assert "except Exception:" in append_block
    assert "append_turn_journal_event(" in helper_src
    assert "except Exception:" in helper_src
    assert "Failed to append submitted turn journal event" in helper_src
