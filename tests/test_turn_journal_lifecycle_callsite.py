from pathlib import Path


def test_streaming_appends_worker_started_before_running_phase():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    run_idx = src.index("def _run_agent_streaming(")
    worker_idx = src.index('"event": "worker_started"', run_idx)
    running_idx = src.index('update_active_run(stream_id, phase="running"', run_idx)

    assert worker_idx < running_idx


def test_streaming_appends_assistant_started_before_final_save():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    save_idx = src.index("s.save()", assistant_idx)

    assert block_idx < assistant_idx < save_idx


def test_streaming_assistant_started_uses_latest_assistant_message():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    block = src[block_idx:assistant_idx]

    assert "range(len(s.messages) - 1, -1, -1)" in block
    assert '"assistant_message_index": _latest_assistant_idx' in src[assistant_idx:src.index("s.save()", assistant_idx)]


def test_streaming_appends_completed_after_final_save():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    assistant_idx = src.index('"event": "assistant_started"')
    save_idx = src.index("s.save()", assistant_idx)
    completed_idx = src.index('"event": "completed"', save_idx)

    assert save_idx < completed_idx


def test_streaming_appends_interrupted_on_provider_error_path():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    err_idx = src.index("err_str = str(e)")
    interrupted_idx = src.index('"event": "interrupted"', err_idx)
    apperror_idx = src.index("put('apperror'", interrupted_idx)

    assert err_idx < interrupted_idx < apperror_idx
