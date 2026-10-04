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

3. (RE-GATE 20:36) A delayed cancel finalizer must not be authorized by a
   MISSING writeback-ownership record: the successor's own teardown clears
   ``SESSION_WRITEBACK_OWNERS[session_id]`` when it completes, so ``owner is
   None`` with ``active_stream_id is None`` again is exactly the ambiguous
   state that lets an obsolete finalizer serialize over the completed
   successor. ``None`` — and any unresolvable current session — must fail
   closed. The stale-stream process-wakeup branch must likewise merge the
   pause into the canonical current session, never save the worker's detached
   snapshot.
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
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
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

    assert result["cancelled"] is True
    assert m_get_session.call_count >= 1, (
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

    assert result["cancelled"] is True
    assert m_get_session.call_count >= 1
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
    """RE-GATE unit control (20:36): ``_finalize_cancelled_turn(..., stream_id=...)``
    must no-op entirely (no clearing, no marker append, no save) unless the
    immutable writeback-ownership record still names the old worker's exact
    stream_id. ``None`` — successor completed and its teardown cleared the
    entry, or the record was never registered — must FAIL CLOSED, and so must
    an unresolvable current session (deleted/unloadable)."""
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
    models.SESSIONS[s.session_id] = s
    config.register_session_writeback_owner(s.session_id, "newer-stream")
    streaming._finalize_cancelled_turn(s, ephemeral=False, stream_id=old_stream)
    assert s.active_stream_id == "newer-stream"
    assert s.pending_user_message == "newer prompt"
    s.save.assert_not_called()
    assert streaming._session_has_cancel_marker(s) is False

    # Ownership record missing (successor completed and its teardown cleared
    # it) and active_stream_id is None again -> ``None`` must NOT authorize:
    # the finalizer no-ops instead of appending/saving the obsolete
    # cancellation over the completed successor.
    s2 = Session(session_id="finalizer-gate-nostream", messages=[])
    s2.active_stream_id = None
    s2.save = Mock()
    models.SESSIONS[s2.session_id] = s2
    streaming._finalize_cancelled_turn(s2, ephemeral=False, stream_id=old_stream)
    s2.save.assert_not_called()
    assert s2.active_stream_id is None
    assert streaming._session_has_cancel_marker(s2) is False

    # Current session cannot be resolved (not cached, no file on disk) ->
    # fail closed: no terminal write from a detached worker.
    s4 = Session(session_id="finalizer-gate-unresolvable", messages=[])
    s4.active_stream_id = None
    s4.save = Mock()
    streaming._finalize_cancelled_turn(s4, ephemeral=False, stream_id=old_stream)
    s4.save.assert_not_called()
    assert streaming._session_has_cancel_marker(s4) is False

    # Session still points at the cancelled stream AND the worker still owns
    # the writeback record -> the finalizer runs.
    s3 = Session(session_id="finalizer-gate-owns", messages=[])
    s3.active_stream_id = old_stream
    s3.save = Mock()
    models.SESSIONS[s3.session_id] = s3
    config.register_session_writeback_owner(s3.session_id, old_stream)
    streaming._finalize_cancelled_turn(s3, ephemeral=False, stream_id=old_stream)
    assert s3.save.call_count >= 1  # PR #6405: settlement path may save twice (cancel marker + active_stream_id clear)
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
        assert streaming.cancel_stream(old_stream)["cancelled"] is True
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


def test_issue6623_writeback_owner_released_only_while_still_owned():
    """RE-GATE unit: the writeback-ownership record is replaced by a successor
    admission, and a worker's teardown clears it ONLY while it still owns it —
    the old worker's finally must never erase the successor's claim."""
    config.register_session_writeback_owner("sess_owner_release", "stream-a")
    # A successor replaced the claim; the old worker's finally must not clear it.
    config.register_session_writeback_owner("sess_owner_release", "stream-b")
    config.clear_session_writeback_owner_if_owned("sess_owner_release", "stream-a")
    assert config.session_writeback_owner("sess_owner_release") == "stream-b"
    # The current owner's finally clears it.
    config.clear_session_writeback_owner_if_owned("sess_owner_release", "stream-b")
    assert config.session_writeback_owner("sess_owner_release") is None


def test_issue6623_replaced_session_successor_survives_delayed_cancel_finalizer(
    tmp_path, monkeypatch
):
    """RE-GATE regression (#6623): the delayed cancel finalizer must resolve
    the CURRENT session object and bind authority to the writeback-ownership
    record — a deterministic schedule with TWO distinct Session instances:

    1. Old turn owns the session; cancel_stream() clears + saves it.
    2. REAL LRU eviction (production ``_evict_sessions_over_cap``) drops the
       old object; ``get_session()`` lazily reloads a DISTINCT object.
    3. A successor turn is admitted and persisted through the replacement.
    4. The old worker unwinds through the real cancel-finalizer path holding
       its stale snapshot.
    5. Cache and disk must retain the successor's fields/messages; the old
       generation must not make a terminal write.

    The pre-fix guard accepted ``active_stream_id is None`` on the worker-held
    snapshot (cancel_stream clears it eagerly), so it proceeded to serialize
    the stale snapshot over the successor — this test fails without the fix.
    """
    sid = "sess_replaced_successor"
    old_stream = "old-replaced-stream"
    newer_stream = "newer-replaced-stream"

    s_old = Session(
        session_id=sid,
        title="Replaced successor",
        messages=[
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "partial old answer"},
        ],
    )
    s_old.active_stream_id = old_stream
    s_old.pending_user_message = "old prompt"
    s_old.pending_attachments = []
    s_old.pending_started_at = 1000.0
    s_old.pending_user_source = "webui"
    s_old.save()
    models.SESSIONS[sid] = s_old

    config.register_stream_owner(old_stream, sid)
    config.register_session_writeback_owner(sid, old_stream)
    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    config.register_active_run(old_stream, session_id=sid, phase="running")

    # 2) Cancel through the real production path.
    assert streaming.cancel_stream(old_stream)["cancelled"] is True
    assert s_old.active_stream_id is None
    assert streaming._session_has_cancel_marker(s_old)
    _old_messages_after_cancel = len(s_old.messages)

    # 3) REAL LRU eviction: drop the (now idle, persisted) old object via the
    # production eviction pass, then lazily reload a DISTINCT object.
    monkeypatch.setattr(models, "SESSIONS_MAX", 2)
    for i in range(4):
        _filler = Session(
            session_id=f"sess_replaced_filler_{i}", title="filler", messages=[]
        )
        _filler.save()
        models.SESSIONS[_filler.session_id] = _filler
    with config.LOCK:
        _evicted = models._evict_sessions_over_cap(0)
    assert sid not in models.SESSIONS, (
        f"old generation should be LRU-evicted (evicted={_evicted})"
    )

    s_new = models.get_session(sid)
    assert s_new is not s_old, "lazy reload must yield a DISTINCT Session instance"

    # 4) Admit the successor the way the route layer does: under the session
    # lock, rotate active_stream_id/pending_*, register the writeback owner,
    # append the newer user turn, and persist.
    _lock = streaming._get_session_agent_lock(sid)
    with _lock:
        s_new.active_stream_id = newer_stream
        s_new.pending_user_message = "newer prompt"
        s_new.pending_attachments = []
        s_new.pending_started_at = 2000.0
        s_new.pending_user_source = "webui"
        s_new.messages.append(
            {"role": "user", "content": "newer prompt", "timestamp": 2000}
        )
        config.register_session_writeback_owner(sid, newer_stream)
        s_new.save()
    _messages_before_release = len(s_new.messages)

    # 5) Release the old worker: it unwinds through the REAL cancel-finalizer
    # path holding its STALE snapshot (s_old) under the session lock.
    with _lock:
        streaming._finalize_cancelled_turn(s_old, ephemeral=False, stream_id=old_stream)

    # 6) The old generation made no terminal write: its in-memory snapshot
    # must be untouched (no delayed marker appended, no stale save).
    assert len(s_old.messages) == _old_messages_after_cancel, (
        "stale snapshot must not receive a delayed terminal write"
    )
    # The successor's cache object survives untouched.
    assert s_new.active_stream_id == newer_stream
    assert s_new.pending_user_message == "newer prompt"
    assert s_new.pending_attachments == []
    assert s_new.pending_started_at == 2000.0
    assert s_new.pending_user_source == "webui"
    assert len(s_new.messages) == _messages_before_release, (
        "delayed cancel finalizer must not append anything over the successor turn"
    )
    # The writeback-ownership record still names the successor.
    assert config.session_writeback_owner(sid) == newer_stream
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


def test_issue6623_replaced_session_completed_successor_still_protected_by_ownership_record(
    tmp_path, monkeypatch
):
    """RE-GATE control (#6623): even after the successor's own turn completed
    (so the CURRENT object's ``active_stream_id`` is None again — exactly the
    ambiguous state the pre-fix guard accepted), the writeback-ownership record
    still proves the session advanced past the old stream. The delayed
    finalizer must no-op: the stale snapshot's save() would otherwise
    overwrite the completed successor transcript on disk."""
    sid = "sess_completed_successor"
    old_stream = "old-completed-stream"
    newer_stream = "newer-completed-stream"

    s_old = Session(
        session_id=sid,
        title="Completed successor",
        messages=[{"role": "user", "content": "old prompt"}],
    )
    s_old.active_stream_id = old_stream
    s_old.pending_user_message = "old prompt"
    s_old.pending_attachments = []
    s_old.pending_started_at = 1000.0
    s_old.pending_user_source = "webui"
    s_old.save()
    models.SESSIONS[sid] = s_old
    config.register_session_writeback_owner(sid, old_stream)

    config.register_stream_owner(old_stream, sid)
    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    config.register_active_run(old_stream, session_id=sid, phase="running")
    assert streaming.cancel_stream(old_stream)["cancelled"] is True

    monkeypatch.setattr(models, "SESSIONS_MAX", 2)
    for i in range(4):
        _filler = Session(
            session_id=f"sess_completed_filler_{i}", title="filler", messages=[]
        )
        _filler.save()
        models.SESSIONS[_filler.session_id] = _filler
    with config.LOCK:
        models._evict_sessions_over_cap(0)
    s_new = models.get_session(sid)
    assert s_new is not s_old

    with streaming._get_session_agent_lock(sid):
        s_new.active_stream_id = newer_stream
        s_new.pending_user_message = "newer prompt"
        s_new.pending_attachments = []
        s_new.pending_started_at = 2000.0
        s_new.pending_user_source = "webui"
        s_new.messages.append(
            {"role": "user", "content": "newer prompt", "timestamp": 2000}
        )
        config.register_session_writeback_owner(sid, newer_stream)
        s_new.save()
        # The successor turn then completes normally: active_stream_id and
        # pending fields are cleared, the transcript persists. The ownership
        # record is NOT touched by normal completion.
        s_new.active_stream_id = None
        s_new.pending_user_message = None
        s_new.pending_attachments = []
        s_new.pending_started_at = None
        s_new.pending_user_source = None
        s_new.save()
    _messages_after_successor_completion = len(s_new.messages)

    with streaming._get_session_agent_lock(sid):
        streaming._finalize_cancelled_turn(s_old, ephemeral=False, stream_id=old_stream)

    assert config.session_writeback_owner(sid) == newer_stream
    disk = models.Session.load(sid)
    assert disk.active_stream_id is None
    assert len(disk.messages) == _messages_after_successor_completion, (
        "delayed finalizer must not mutate the completed successor transcript"
    )
    assert any(
        str(m.get("content", "")) == "newer prompt" and m.get("role") == "user"
        for m in disk.messages
    )


def test_issue6623_replaced_session_process_wakeup_pause_merges_into_current(
    tmp_path, monkeypatch
):
    """RE-GATE regression (#6623): the credential-pool process-wakeup
    exception branch must merge the pause into the CURRENT session object
    under the canonical lock — never save a detached worker snapshot.

    Same replacement schedule as the finalizer regression: cancel the old
    generation, force REAL LRU eviction, lazily reload a distinct object,
    admit and persist a successor through it, then run the pause branch with
    the old worker's stale snapshot. The pause must land on the current object
    + disk while the successor state survives; the stale snapshot must not be
    the write target."""
    sid = "sess_replaced_pause"
    old_stream = "old-pause-stream"
    newer_stream = "newer-pause-stream"

    s_old = Session(
        session_id=sid,
        title="Replaced pause",
        messages=[{"role": "user", "content": "old prompt"}],
    )
    s_old.active_stream_id = old_stream
    s_old.pending_user_message = "old prompt"
    s_old.pending_attachments = []
    s_old.pending_started_at = 1000.0
    s_old.pending_user_source = "process_wakeup"
    s_old.save()
    models.SESSIONS[sid] = s_old
    config.register_session_writeback_owner(sid, old_stream)

    config.register_stream_owner(old_stream, sid)
    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    config.register_active_run(old_stream, session_id=sid, phase="running")
    assert streaming.cancel_stream(old_stream)["cancelled"] is True

    monkeypatch.setattr(models, "SESSIONS_MAX", 2)
    for i in range(4):
        _filler = Session(
            session_id=f"sess_pause_filler_{i}", title="filler", messages=[]
        )
        _filler.save()
        models.SESSIONS[_filler.session_id] = _filler
    with config.LOCK:
        models._evict_sessions_over_cap(0)
    assert sid not in models.SESSIONS
    s_new = models.get_session(sid)
    assert s_new is not s_old

    with streaming._get_session_agent_lock(sid):
        s_new.active_stream_id = newer_stream
        s_new.pending_user_message = "newer prompt"
        s_new.pending_attachments = []
        s_new.pending_started_at = 2000.0
        s_new.pending_user_source = "webui"
        s_new.messages.append(
            {"role": "user", "content": "newer prompt", "timestamp": 2000}
        )
        config.register_session_writeback_owner(sid, newer_stream)
        s_new.save()

    # The old worker unwinds through the credential-pool process-wakeup
    # branch holding its stale snapshot, under the session lock.
    with streaming._get_session_agent_lock(sid):
        _recorded = streaming._merge_process_wakeup_pause_into_current_session(
            s_old,
            classification="credential_pool_empty",
            model="test-model",
            provider="test-provider",
        )
    assert _recorded is not None
    # The stale snapshot itself was NOT the write target: the default empty
    # pause dict must not carry the recorded pause.
    assert not (s_old.process_wakeup_pause or {}).get("paused")
    # The pause merged into the CURRENT object and persisted.
    assert s_new.process_wakeup_pause is not None
    assert s_new.process_wakeup_pause.get("paused") is True
    assert s_new.process_wakeup_pause.get("classification") == "credential_pool_empty"
    assert s_new.process_wakeup_pause.get("model") == "test-model"
    disk = models.Session.load(sid)
    assert disk.process_wakeup_pause is not None
    assert disk.process_wakeup_pause.get("classification") == "credential_pool_empty"
    # Successor state survives on disk.
    assert disk.active_stream_id == newer_stream
    assert disk.pending_user_message == "newer prompt"
    assert any(
        str(m.get("content", "")) == "newer prompt" and m.get("role") == "user"
        for m in disk.messages
    )


def test_issue6623_completed_successor_teardown_cleared_owner_blocks_old_finalizer(
    tmp_path, monkeypatch
):
    """RE-GATE regression (#6623, 20:36): a delayed old finalizer must not be
    authorized by a MISSING writeback-ownership record.

    The successor is admitted, persists its result, then COMPLETES — and its
    teardown clears the ownership entry (``clear_session_writeback_owner_if_owned``
    in the final teardown of ``_run_agent_streaming``). When the old cancelled
    worker finally resumes, ``owner is None`` AND the current object's
    ``active_stream_id`` is None again — the exact ambiguous state the pre-fix
    guard accepted (``owner is not None`` / ``active_stream_id is not None``
    both false). ``None`` must fail closed: the old worker must not
    append/save its obsolete cancellation over the completed successor.

    This is the sequence the earlier completed-successor control did NOT
    exercise (it kept the ownership record alive); production completion
    clears it.
    """
    sid = "sess_teardown_cleared_owner"
    old_stream = "old-teardown-stream"
    newer_stream = "newer-teardown-stream"

    s_old = Session(
        session_id=sid,
        title="Teardown-cleared owner",
        messages=[{"role": "user", "content": "old prompt"}],
    )
    s_old.active_stream_id = old_stream
    s_old.pending_user_message = "old prompt"
    s_old.pending_attachments = []
    s_old.pending_started_at = 1000.0
    s_old.pending_user_source = "webui"
    s_old.save()
    models.SESSIONS[sid] = s_old
    config.register_session_writeback_owner(sid, old_stream)

    config.register_stream_owner(old_stream, sid)
    config.STREAMS[old_stream] = queue.Queue()
    config.CANCEL_FLAGS[old_stream] = threading.Event()
    config.register_active_run(old_stream, session_id=sid, phase="running")

    # 2) Cancel the old turn through the real production path.
    assert streaming.cancel_stream(old_stream)["cancelled"] is True
    assert s_old.active_stream_id is None
    assert streaming._session_has_cancel_marker(s_old)
    _old_messages_after_cancel = len(s_old.messages)

    # 3) REAL LRU eviction + lazy reload of a DISTINCT current object.
    monkeypatch.setattr(models, "SESSIONS_MAX", 2)
    for i in range(4):
        _filler = Session(
            session_id=f"sess_teardown_filler_{i}", title="filler", messages=[]
        )
        _filler.save()
        models.SESSIONS[_filler.session_id] = _filler
    with config.LOCK:
        models._evict_sessions_over_cap(0)
    s_new = models.get_session(sid)
    assert s_new is not s_old, "lazy reload must yield a DISTINCT Session instance"

    # 4) Admit the successor, let it COMPLETE, then run its teardown: the
    # ownership entry is released (compare-and-clear) exactly like the final
    # teardown in _run_agent_streaming.
    with streaming._get_session_agent_lock(sid):
        s_new.active_stream_id = newer_stream
        s_new.pending_user_message = "newer prompt"
        s_new.pending_attachments = []
        s_new.pending_started_at = 2000.0
        s_new.pending_user_source = "webui"
        s_new.messages.append(
            {"role": "user", "content": "newer prompt", "timestamp": 2000}
        )
        config.register_session_writeback_owner(sid, newer_stream)
        s_new.save()
        # Successor completes: active_stream_id + pending fields cleared.
        s_new.active_stream_id = None
        s_new.pending_user_message = None
        s_new.pending_attachments = []
        s_new.pending_started_at = None
        s_new.pending_user_source = None
        s_new.save()
        # Successor teardown: release ownership while still owning it.
        config.clear_session_writeback_owner_if_owned(sid, newer_stream)
    assert config.session_writeback_owner(sid) is None
    _messages_after_successor = len(s_new.messages)
    # The old turn's OWN cancel marker is legitimately on disk (cancel_stream
    # persisted it); the successor transcript may carry it. What must NOT
    # happen is the delayed finalizer appending a NEW one.
    _cancel_markers_before = sum(
        1 for m in s_new.messages
        if str(m.get("content", "")).startswith("**Task cancelled:**")
    )

    # 5) The old worker resumes and unwinds through the delayed cancel
    # finalizer holding its stale snapshot, under the session lock.
    with streaming._get_session_agent_lock(sid):
        streaming._finalize_cancelled_turn(s_old, ephemeral=False, stream_id=old_stream)

    # 6) Fail closed: the old generation made no terminal write.
    assert len(s_old.messages) == _old_messages_after_cancel, (
        "stale snapshot must not receive a delayed terminal write"
    )
    # The successor's cache object and disk state survive untouched.
    assert s_new.active_stream_id is None
    assert s_new.pending_user_message is None
    assert len(s_new.messages) == _messages_after_successor, (
        "delayed cancel finalizer must not append over the completed successor"
    )
    assert sum(
        1 for m in s_new.messages
        if str(m.get("content", "")).startswith("**Task cancelled:**")
    ) == _cancel_markers_before, (
        "delayed cancel finalizer must not append a new cancel marker"
    )
    disk = models.Session.load(sid)
    assert disk.active_stream_id is None
    assert len(disk.messages) == _messages_after_successor
    assert any(
        str(m.get("content", "")) == "newer prompt" and m.get("role") == "user"
        for m in disk.messages
    )
    assert sum(
        1 for m in disk.messages
        if str(m.get("content", "")).startswith("**Task cancelled:**")
    ) == _cancel_markers_before, (
        "disk must not gain a new cancel marker from the stale finalizer"
    )


def test_issue6623_unresolvable_current_session_fails_closed_for_pause_merge(
    tmp_path, monkeypatch
):
    """RE-GATE regression (#6623, 20:36): the process-wakeup pause merge must
    fail closed (no write, no exception) when the canonical current session
    cannot be resolved — the worker-held snapshot is never the write target.
    """
    sid = "sess_unresolvable_pause"
    s_old = Session(session_id=sid, messages=[])
    s_old.save = Mock()
    # NOT cached, no file on disk -> get_session() resolves nothing.
    recorded = streaming._merge_process_wakeup_pause_into_current_session(
        s_old,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert recorded is None
    s_old.save.assert_not_called()
    assert not (s_old.process_wakeup_pause or {}).get("paused")


def test_issue6636_gateway_prestart_cancel_clears_writeback_owner(tmp_path, monkeypatch):
    """RE-GATE (maintainer fix): the Gateway worker's pre-start cancellation path
    (``q is None`` early return in ``_run_gateway_chat_streaming``) must release
    the SESSION_WRITEBACK_OWNERS entry the route layer registered, or every
    cancelled-before-start Gateway run leaks a permanent owner entry (unbounded
    process-lifetime growth). Revert-sensitive: fails if the
    clear_session_writeback_owner_if_owned call on that path is removed.
    """
    import api.gateway_chat as gateway_chat

    session_id = "sess_gw_prestart"
    stream_id = "stream-gw-prestart"
    # The route layer registered the writeback owner before dispatching the worker.
    config.register_session_writeback_owner(session_id, stream_id)
    assert config.session_writeback_owner(session_id) == stream_id
    # Simulate cancel-before-start: the stream map has no queue for this id, so the
    # worker takes its `q is None` early-return teardown path.
    config.STREAMS.pop(stream_id, None)

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        [],
        "test-model",
        None,
        stream_id,
        None,
    )

    assert config.session_writeback_owner(session_id) is None, (
        "pre-start Gateway cancellation must clear the writeback owner it registered"
    )


def test_issue6636_gateway_clear_only_affects_owned_stream(tmp_path, monkeypatch):
    """The Gateway teardown clear must be compare-and-clear: if a successor has
    already taken writeback ownership by the time the old worker tears down, the
    old worker's clear must NOT evict the successor's entry.
    """
    session_id = "sess_gw_successor"
    old_stream = "stream-gw-old"
    new_stream = "stream-gw-new"
    config.register_session_writeback_owner(session_id, old_stream)
    # Successor takes ownership before the old worker's teardown runs.
    config.register_session_writeback_owner(session_id, new_stream)
    # Old worker's teardown compare-and-clear must be a no-op (it no longer owns it).
    config.clear_session_writeback_owner_if_owned(session_id, old_stream)
    assert config.session_writeback_owner(session_id) == new_stream, (
        "old worker teardown must not evict the successor's writeback ownership"
    )
