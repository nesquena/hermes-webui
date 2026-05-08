"""
Regression tests for session streaming indicator payloads used by the session list.

This ensures backend payloads report per-session streaming status from active stream
tracking, not only for the foreground conversation.
"""

import threading

import pytest

import api.models as models
from api.models import Session, all_sessions


@pytest.fixture(autouse=True)
def _isolate_session_stream_state(tmp_path, monkeypatch):
    """Keep session/index/stream state isolated from the host environment."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    stream_map = {}
    stream_lock = threading.Lock()
    monkeypatch.setattr(models, "STREAMS", stream_map)
    monkeypatch.setattr(models, "STREAMS_LOCK", stream_lock)

    yield

    models.SESSIONS.clear()


def _make_session(session_id, stream_id=None, message_count=1):
    s = Session(
        session_id=session_id,
        title=session_id,
        messages=[{"role": "user", "content": f"seed-{session_id}"}] * message_count,
    )
    s.active_stream_id = stream_id
    return s


def test_all_sessions_marks_indexed_and_in_memory_streaming_sessions():
    """Session records from both index and in-memory cache should expose is_streaming."""
    s_disk = _make_session("disk_session", stream_id="stream-1")
    s_disk.pending_user_message = "pending disk turn"
    s_disk.save()

    s_memory = _make_session("memory_session", stream_id="stream-2")
    s_memory.pending_user_message = "pending memory turn"
    with models.LOCK:
        models.SESSIONS[s_memory.session_id] = s_memory

    models.STREAMS["stream-1"] = object()
    models.STREAMS["stream-2"] = object()

    listed = all_sessions()
    by_sid = {s["session_id"]: s for s in listed}

    assert by_sid["disk_session"]["is_streaming"] is True
    assert by_sid["memory_session"]["is_streaming"] is True
    assert by_sid["memory_session"]["active_stream_id"] == "stream-2"
    assert by_sid["disk_session"]["pending_user_message"] == "pending disk turn"
    assert by_sid["memory_session"]["pending_user_message"] == "pending memory turn"


def test_all_sessions_marks_streaming_false_when_stream_is_not_active():
    """Stale active_stream_id should not imply streaming without active STREAMS entry."""
    s = _make_session("stalesession", stream_id="stale-stream")
    s.save()

    assert all_sessions()[0]["is_streaming"] is False

    models.STREAMS["stale-stream"] = object()
    assert all_sessions()[0]["is_streaming"] is True

    models.STREAMS.pop("stale-stream", None)
    assert all_sessions()[0]["is_streaming"] is False


def test_all_sessions_does_not_report_streaming_after_restart_without_active_registry():
    """Server restarts should not resurrect sidebar streaming state from disk alone."""
    s = _make_session("restart_session", stream_id="old-stream")
    s.save()

    models.SESSIONS.clear()
    reloaded = Session.load("restart_session")
    assert reloaded is not None
    assert reloaded.active_stream_id == "old-stream"

    listed = all_sessions()
    assert listed[0]["active_stream_id"] == "old-stream"
    assert listed[0]["is_streaming"] is False
