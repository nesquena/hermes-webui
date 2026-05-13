import queue
import threading
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
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
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
