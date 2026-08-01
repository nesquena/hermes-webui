"""Regression tests for #6623: early-Stop cancel race + stale cancelled worker.

Covers the two CHANGES_REQUESTED items on PR #6636:

1. ``cancel_stream()`` must capture the stream owner registry entry WHILE the
   stream still exists (under ``STREAMS_LOCK``, before the eager
   ``STREAMS.pop``). The just-starting worker takes its
   ``q is None -> unregister_stream_owner`` early path the instant the stream
   map entry disappears, so a post-pop owner lookup races that teardown and
   returns None — leaving ``session.active_stream_id`` / pending_* stuck while
   the HTTP cancel path still reports success.

2. A worker stuck in C-level I/O may never reach its ``finally`` to unregister
   the run, so ``ACTIVE_RUNS`` can hold the row forever and
   ``_clear_stale_stream_state()`` defers stale cleanup indefinitely. The fix
   stamps ``cancelled_at`` on the run when cancel_stream() flips it to
   phase="cancelling", and ``_clear_stale_stream_state()`` reclaims the session
   once the cancel has been outstanding past the grace window.
"""
import queue
import threading
import time

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from unittest.mock import Mock, patch


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
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()


class _PoppingStreams(dict):
    """STREAMS stand-in that retires the stream owner the instant the stream
    entry is popped — exactly what the just-starting worker does in its
    ``q is None -> unregister_stream_owner`` early path once cancel_stream()
    eagerly pops ``STREAMS[stream_id]``."""

    def pop(self, key, *default):
        value = dict.pop(self, key, *default)
        config.unregister_stream_owner(key)
        return value


def test_issue6623_owner_must_be_captured_before_stream_pop(tmp_path, monkeypatch):
    """Deterministic repro of the nesquena-hermes interleaving:

    1. Stop sees the stream while AGENT_INSTANCES and ACTIVE_RUNS are empty.
    2. Stop removes STREAMS[stream_id] (via the _PoppingStreams wrapper, which
       synchronously retires the owner exactly like the worker's early path).
    3. Stop then resolves the owner — the post-pop lookup would return None,
       skipping session cleanup while cancel still returns True.

    The fix must capture the owner under the lock BEFORE the pop, so session
    cleanup still runs (get_session called exactly once) and the persisted
    active_stream_id / pending fields are cleared.
    """
    sid = "sess_owner_race"
    stream_id = "stream_owner_race"

    s = Session(session_id=sid, title="Owner race", messages=[])
    s.active_stream_id = stream_id
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1234567890.0
    s.save = Mock()
    models.SESSIONS[sid] = s

    # Real owner registration, exactly like the route layer before worker start.
    config.register_stream_owner(stream_id, sid)

    # STREAMS present; NO AGENT_INSTANCES, NO ACTIVE_RUNS (early-Stop race).
    wrapper = _PoppingStreams()
    wrapper[stream_id] = queue.Queue()
    monkeypatch.setattr(config, "STREAMS", wrapper)
    monkeypatch.setattr(streaming, "STREAMS", wrapper)
    config.CANCEL_FLAGS[stream_id] = threading.Event()

    with patch("api.streaming.get_session", return_value=s) as m_get_session:
        result = streaming.cancel_stream(stream_id)

    assert result is True
    assert m_get_session.call_count == 1, (
        f"Expected 'get_session' to be called once. Called {m_get_session.call_count} times. "
        "The stream owner must be captured BEFORE the STREAMS pop."
    )
    assert s.active_stream_id is None
    assert s.pending_user_message is None
    assert s.pending_attachments == []
    assert s.pending_started_at is None
    s.save.assert_called_once()
    # No owner leak: the simulated worker teardown retired the registry entry,
    # and cancel captured the owner before that teardown could hide it.
    assert config.STREAM_SESSION_OWNERS.get(stream_id) is None


def test_issue6623_newer_stream_stale_writeback_still_rejected(tmp_path, monkeypatch):
    """Control: with the owner captured before the pop, a session whose
    active_stream_id has rotated to a NEWER stream must still be left alone —
    _stream_writeback_is_current() keeps rejecting the stale writeback, no
    cancel marker is appended, and no save happens."""
    sid = "sess_owner_rotated"
    stream_id = "old-stream-owner-race"

    s = Session(
        session_id=sid,
        title="Rotated stream",
        messages=[{"role": "user", "content": "newer prompt"}],
    )
    s.active_stream_id = "newer-stream"
    s.pending_user_message = "newer prompt"
    s.pending_started_at = 456.0
    s.save = Mock()
    models.SESSIONS[sid] = s

    config.register_stream_owner(stream_id, sid)

    wrapper = _PoppingStreams()
    wrapper[stream_id] = queue.Queue()
    monkeypatch.setattr(config, "STREAMS", wrapper)
    monkeypatch.setattr(streaming, "STREAMS", wrapper)
    config.CANCEL_FLAGS[stream_id] = threading.Event()

    with patch("api.streaming.get_session", return_value=s) as m_get_session:
        result = streaming.cancel_stream(stream_id)

    assert result is True
    assert m_get_session.call_count == 1
    assert s.active_stream_id == "newer-stream"
    assert s.pending_user_message == "newer prompt"
    s.save.assert_not_called()
    assert all(
        str(m.get("content", "")) != "*Task cancelled.*" for m in s.messages
    ), "stale cancel writeback must still be rejected for a newer stream"
    assert config.STREAM_SESSION_OWNERS.get(stream_id) is None


def test_issue6623_stale_cancelled_run_cleared_after_grace(tmp_path, monkeypatch):
    """A cancelled run whose cancel has been outstanding past the grace window
    is treated as stale: _clear_stale_stream_state() clears the session even
    though the worker row is still in ACTIVE_RUNS (worker stuck in C-level I/O
    never reached its finally)."""
    import api.routes as routes

    sid = "stale_cancelled_sid"
    stream_id = "stale-cancelled-stream"
    s = Session(session_id=sid, title="Stale cancelled", messages=[])
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[sid] = s

    config.register_active_run(
        stream_id,
        session_id=sid,
        phase="cancelling",
        cancelled_at=time.time() - 120.0,
    )

    assert routes._clear_stale_stream_state(s) is True
    assert s.active_stream_id is None


def test_issue6623_stale_cancelled_run_without_cancelled_at_reclaimed_via_started_at(
    tmp_path, monkeypatch
):
    """Legacy run cancelled before the cancelled_at stamp existed: the
    started_at anchor still reclaims the session once the run is old enough."""
    import api.routes as routes

    sid = "legacy_stale_cancelled_sid"
    stream_id = "legacy-stale-cancelled-stream"
    s = Session(session_id=sid, title="Legacy stale cancelled", messages=[])
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[sid] = s

    config.register_active_run(stream_id, session_id=sid, phase="cancelling")
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[stream_id]["started_at"] = time.time() - 120.0

    assert routes._clear_stale_stream_state(s) is True
    assert s.active_stream_id is None


def test_issue6623_fresh_cancelled_run_still_deferred(tmp_path, monkeypatch):
    """Control: a recently cancelled run (inside the grace window) must still
    defer stale cleanup — the worker may legitimately be unwinding."""
    import api.routes as routes

    sid = "fresh_cancelled_sid"
    stream_id = "fresh-cancelled-stream"
    s = Session(session_id=sid, title="Fresh cancelled", messages=[])
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[sid] = s

    config.register_active_run(
        stream_id,
        session_id=sid,
        phase="cancelling",
        cancelled_at=time.time() - 5.0,
    )

    assert routes._clear_stale_stream_state(s) is False
    assert s.active_stream_id == stream_id
