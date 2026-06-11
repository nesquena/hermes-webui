import queue
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()


def test_stream_writeback_requires_active_stream_ownership():
    s = Session(session_id="ownership", messages=[])
    s.active_stream_id = "current-stream"

    assert streaming._stream_writeback_is_current(s, "current-stream") is True

    s.active_stream_id = None
    assert streaming._stream_writeback_is_current(s, "current-stream") is False

    s.active_stream_id = "newer-stream"
    assert streaming._stream_writeback_is_current(s, "current-stream") is False


def test_cancel_stream_does_not_append_marker_after_stream_ownership_rotated():
    sid = "rotated_cancel_sid"
    old_stream = "old-stream"
    s = Session(
        session_id=sid,
        title="Rotated stream",
        messages=[{"role": "user", "content": "newer prompt"}],
    )
    s.active_stream_id = "newer-stream"
    s.pending_user_message = "newer prompt"
    s.pending_started_at = 456.0
    s.save()
    models.SESSIONS[sid] = s

    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    mock_agent = Mock()
    mock_agent.session_id = sid
    mock_agent.interrupt = Mock()
    config.AGENT_INSTANCES[old_stream] = mock_agent

    assert streaming.cancel_stream(old_stream) is True

    assert s.active_stream_id == "newer-stream"
    assert s.pending_user_message == "newer prompt"
    assert [m["content"] for m in s.messages] == ["newer prompt"]
    assert all(m.get("content") != "*Task cancelled.*" for m in s.messages)


def test_stale_stream_clear_skips_active_worker_when_sse_channel_is_gone():
    import api.routes as routes

    sid = "active_worker_missing_sse"
    stream_id = "live-worker-stream"
    s = Session(
        session_id=sid,
        title="Active worker missing SSE",
        messages=[{"role": "user", "content": "previous prompt"}],
    )
    s.active_stream_id = stream_id
    s.pending_user_message = "new prompt"
    s.pending_started_at = time.time()
    s.save()
    models.SESSIONS[sid] = s

    config.register_active_run(stream_id, session_id=sid, phase="running")

    assert routes._clear_stale_stream_state(s) is False

    assert s.active_stream_id == stream_id
    assert s.pending_user_message == "new prompt"
    assert s.pending_started_at is not None
    assert [m["content"] for m in s.messages] == ["previous prompt"]
    assert all(not m.get("_error") for m in s.messages)


def test_stale_stream_clear_skips_fresh_pending_turn_inside_grace_window(monkeypatch):
    import api.routes as routes

    sid = "fresh_pending_missing_sse"
    stream_id = "fresh-pending-stream"
    s = Session(
        session_id=sid,
        title="Fresh pending missing SSE",
        messages=[{"role": "user", "content": "previous prompt"}],
    )
    s.active_stream_id = stream_id
    s.pending_user_message = "new prompt"
    s.pending_started_at = 1000.0
    s.save()
    models.SESSIONS[sid] = s
    monkeypatch.setattr(routes.time, "time", lambda: 1005.0)

    assert routes._clear_stale_stream_state(s) is False

    assert s.active_stream_id == stream_id
    assert s.pending_user_message == "new prompt"
    assert s.pending_started_at == 1000.0
    assert [m["content"] for m in s.messages] == ["previous prompt"]
    assert all(not m.get("_error") for m in s.messages)


def test_stale_stream_clear_trusts_completed_run_journal_instead_of_adding_marker(monkeypatch):
    import api.routes as routes
    from api.run_journal import append_run_event

    sid = "completed_journal_late_pending_clear"
    stream_id = "completed-stream"
    s = Session(
        session_id=sid,
        title="Completed journal late pending clear",
        messages=[
            {"role": "user", "content": "previous prompt"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "new prompt"},
            {"role": "assistant", "content": "finished answer"},
        ],
    )
    s.active_stream_id = stream_id
    s.pending_user_message = "new prompt"
    s.pending_started_at = 1000.0
    s.save()
    models.SESSIONS[sid] = s
    append_run_event(sid, stream_id, "done", {"session": {"session_id": sid}})
    monkeypatch.setattr(routes.time, "time", lambda: 1400.0)

    assert routes._clear_stale_stream_state(s) is True

    assert s.active_stream_id is None
    assert s.pending_user_message is None
    assert s.pending_started_at is None
    assert [m["content"] for m in s.messages] == [
        "previous prompt",
        "previous answer",
        "new prompt",
        "finished answer",
    ]
    assert all("Response interrupted" not in str(m.get("content") or "") for m in s.messages)
    assert all(not m.get("_error") for m in s.messages)


def test_success_path_checks_stream_ownership_before_persisting_result():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    guard = "if not ephemeral and not _stream_writeback_is_current(s, stream_id):"
    guard_pos = src.find(guard)
    result_merge_pos = src.find("_result_messages = result.get('messages') or _previous_context_messages")
    compression_pos = src.find("Handle context compression side effects")

    assert guard_pos != -1
    assert result_merge_pos != -1
    assert compression_pos != -1
    assert guard_pos < result_merge_pos
    assert guard_pos < compression_pos


def test_self_heal_retry_success_checks_stream_ownership_before_writeback():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    start = src.index("logger.info('[webui] self-heal (except path): retrying stream")
    end = src.index("logger.info('[webui] self-heal (except path): retry succeeded')", start)
    block = src[start:end]
    guard = "if not ephemeral and not _stream_writeback_is_current(s, stream_id):"

    assert guard in block
    assert block.index(guard) < block.index("_result_messages = _heal_result.get('messages') or _previous_context_messages")
    assert block.index(guard) < block.index("s.save()")


def test_outer_exception_path_checks_stream_ownership_before_error_writeback():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    outer_error_payload = src.index("_error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)")
    start = src.index("# Persist the error so it survives page reload.", outer_error_payload)
    end = src.index("put('apperror', _error_payload)", start)
    block = src[start:end]
    guard = "if not ephemeral and not _stream_writeback_is_current(s, stream_id):"

    assert guard in block
    assert block.index(guard) < block.index("_materialize_pending_user_turn_before_error(s)")
    assert block.index(guard) < block.index("s.active_stream_id = None")
    assert block.index(guard) < block.index("s.messages.append(_error_message)")
