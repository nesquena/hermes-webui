from pathlib import Path


def test_chat_start_appends_submitted_turn_journal_before_worker_thread_start():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    start_idx = src.index("def _start_chat_stream_for_session(")
    save_idx = src.index("_prepare_chat_start_session_for_stream(", start_idx)
    append_idx = src.index("append_turn_journal_event(", save_idx)
    thread_idx = src.index("threading.Thread(", append_idx)

    assert save_idx < append_idx < thread_idx
    assert '"event": "submitted"' in src[append_idx:thread_idx]
    assert '"role": "user"' in src[append_idx:thread_idx]
