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


def test_issue6623_delayed_cancel_finalizer_gated_by_stream_ownership():
    """RE-GATE unit control: ``_finalize_cancelled_turn(..., stream_id=...)``
    must no-op entirely (no clearing, no marker append, no save) once a
    successor owns the session — while still finalizing when the session
    points at the cancelled stream or at no stream at all."""
    old_stream = "finalizer-old-stream"

    # Successor owns the session -> the delayed finalizer must no-op.
    s = Session(
        session_id="finalizer-gate-successor",
        messages=[{"role": "user", "content": "newer prompt"}],
    )
    s.active_stream_id = "newer-stream"
    s.pending_user_message = "newer prompt"
    s.pending_started_at = 456.0
    s.save = Mock()
    streaming._finalize_cancelled_turn(s, ephemeral=False, stream_id=old_stream)
    assert s.active_stream_id == "newer-stream"
    assert s.pending_user_message == "newer prompt"
    s.save.assert_not_called()
    assert streaming._session_has_cancel_marker(s) is False

    # No stream owns the session (cancel_stream already cleared it, no
    # successor) -> the finalizer may still persist the cancelled-turn marker.
    s2 = Session(session_id="finalizer-gate-nostream", messages=[])
    s2.active_stream_id = None
    s2.save = Mock()
    streaming._finalize_cancelled_turn(s2, ephemeral=False, stream_id=old_stream)
    s2.save.assert_called_once()
    assert s2.active_stream_id is None
    assert streaming._session_has_cancel_marker(s2) is True

    # Session still points at the cancelled stream -> the finalizer runs.
    s3 = Session(session_id="finalizer-gate-owns", messages=[])
    s3.active_stream_id = old_stream
    s3.save = Mock()
    streaming._finalize_cancelled_turn(s3, ephemeral=False, stream_id=old_stream)
    s3.save.assert_called_once()
    assert s3.active_stream_id is None
    assert streaming._session_has_cancel_marker(s3) is True


def test_issue6623_recent_cancel_not_reaped_despite_old_started_at(tmp_path, monkeypatch):
    """RE-GATE control for the cancellation-age anchor: a just-cancelled turn
    whose ORIGINAL started_at is far past the 180s unwind ceiling must NOT be
    reaped (and a successor admitted) — cancel_stream() removes STREAMS
    itself, so absence from STREAMS is not proof the worker is dead. The
    reaping anchor for a phase="cancelling" row is the cancel time."""
    import api.routes as routes

    sid = "recent_cancel_old_start"
    old_stream = "recent-cancel-old-start-stream"
    config.register_active_run(
        old_stream,
        session_id=sid,
        started_at=time.time() - 400.0,
        phase="cancelling",
        cancelled_at=time.time() - 5.0,
    )

    assert routes._active_run_stream_for_session(sid) == old_stream
    assert old_stream in config.ACTIVE_RUNS


def test_issue6623_stale_recovery_successor_survives_delayed_cancel_finalizer(
    tmp_path, monkeypatch
):
    """RE-GATE production-composed regression (#6623): a long-running worker
    that is cancelled, reaped by stale recovery, and superseded must not
    clobber the successor when it finally returns from the provider boundary
    and unwinds through the delayed cancel finalizer.

    Sequence, all through real production code paths:

    1. Old turn owns the session with an ACTIVE_RUNS row whose original
       started_at is long past the 180s unwind ceiling.
    2. cancel_stream() pops STREAMS, stamps phase="cancelling" + cancelled_at,
       clears the session, writes the cancel marker, and saves.
    3. The cancel stays outstanding past the 180s ceiling (worker stuck in
       provider I/O) -> _active_run_stream_for_session() reaps the row and a
       successor turn is admitted (active_stream_id/pending_* rotated, newer
       user message appended, persisted).
    4. The old worker is released from the provider boundary and runs the
       delayed cancel finalizer under the session lock — exactly the call
       sites at api/streaming.py:9686/9747.
    5. The successor's active_stream_id, pending fields, messages, and saved
       state must all survive.
    """
    import api.routes as routes

    sid = "sess_stale_successor"
    old_stream = "old-stale-cancelled-stream"
    newer_stream = "newer-successor-stream"

    s = Session(
        session_id=sid,
        title="Stale successor",
        messages=[
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "partial old answer"},
        ],
    )
    s.active_stream_id = old_stream
    s.pending_user_message = "old prompt"
    s.pending_attachments = []
    s.pending_started_at = 1000.0
    s.pending_user_source = "webui"
    s.save()
    models.SESSIONS[sid] = s

    config.register_stream_owner(old_stream, sid)
    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    config.register_active_run(
        old_stream,
        session_id=sid,
        started_at=time.time() - 400.0,  # original run far past the 180s ceiling
        phase="running",
    )

    # 2) Cancel through the real production path.
    with patch("api.streaming.get_session", return_value=s):
        assert streaming.cancel_stream(old_stream) is True
    assert s.active_stream_id is None
    assert streaming._session_has_cancel_marker(s)

    # 3) Stale recovery: the cancel has been outstanding past the unwind
    # ceiling (stuck worker never reached its finally) -> the run row is
    # reaped and the successor may be admitted.
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[old_stream]["cancelled_at"] = time.time() - 200.0
    assert routes._active_run_stream_for_session(sid) is None
    assert old_stream not in config.ACTIVE_RUNS

    # Admit the successor the way the route layer does: under the session
    # lock, rotate active_stream_id/pending_*, append the newer user turn,
    # and persist.
    _lock = streaming._get_session_agent_lock(sid)
    with _lock:
        s.active_stream_id = newer_stream
        s.pending_user_message = "newer prompt"
        s.pending_attachments = []
        s.pending_started_at = 2000.0
        s.pending_user_source = "webui"
        s.messages.append(
            {"role": "user", "content": "newer prompt", "timestamp": 2000}
        )
        s.save()
    _messages_before_release = len(s.messages)

    # 4) Release the old worker from the provider boundary; it unwinds through
    # the delayed cancel finalizer under the session lock.
    _errors = []
    _release = threading.Event()

    def _old_worker_return():
        _release.wait()  # the provider boundary the worker was blocked in
        try:
            with _lock:
                streaming._finalize_cancelled_turn(
                    s, ephemeral=False, stream_id=old_stream
                )
        except Exception as exc:  # pragma: no cover - failure surface
            _errors.append(exc)

    _worker = threading.Thread(target=_old_worker_return, daemon=True)
    _worker.start()
    _release.set()
    _worker.join(timeout=10)
    assert not _worker.is_alive(), "old worker thread did not unwind"
    assert not _errors

    # 5) The successor's state must survive untouched.
    assert s.active_stream_id == newer_stream
    assert s.pending_user_message == "newer prompt"
    assert s.pending_attachments == []
    assert s.pending_started_at == 2000.0
    assert s.pending_user_source == "webui"
    assert len(s.messages) == _messages_before_release, (
        "delayed cancel finalizer must not append anything over the successor turn"
    )
    assert any(
        str(m.get("content", "")) == "newer prompt" and m.get("role") == "user"
        for m in s.messages
    )
    # The persisted state survives on disk too.
    disk = models.Session.load(sid)
    assert disk.active_stream_id == newer_stream
    assert disk.pending_user_message == "newer prompt"
    assert disk.pending_started_at == 2000.0
    assert disk.pending_user_source == "webui"
    assert any(
        str(m.get("content", "")) == "newer prompt" and m.get("role") == "user"
        for m in disk.messages
    )
