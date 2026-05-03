"""Regression tests for session sidecar repair logic."""
import json
import queue
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import api.models as models
from api.models import (
    Session,
    _get_profile_home,
    _apply_core_sync_or_error_marker,
    _repair_stale_pending,
    _active_stream_ids,
)
import api.config as config
import api.streaming as streaming
import api.profiles as profiles


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp directory."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    models.SESSIONS.clear()
    yield session_dir, index_file
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    """Isolate shared stream state between tests."""
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    """Clear per-session agent locks between tests."""
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Set up a HERMES_HOME directory with a sessions subdirectory."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    sessions_dir = home / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _make_session(session_id="test_sid", messages=None, **kwargs):
    """Helper to create a Session with sensible defaults for repair tests."""
    defaults = {
        "session_id": session_id,
        "title": "Test Session",
        "messages": messages or [],
    }
    defaults.update(kwargs)
    return Session(**defaults)


def _make_stale_session(session_id="stale_sid", pending_msg="Hello hermes", stream_id="stream_1"):
    """Helper to create a session in stale-pending state (messages empty, pending set)."""
    s = _make_session(session_id=session_id, messages=[])
    s.pending_user_message = pending_msg
    s.active_stream_id = stream_id
    s.pending_attachments = []
    s.pending_started_at = None
    return s


def _write_core_transcript(hermes_home, session_id, messages, **extra):
    """Write a core transcript JSON file for a session."""
    core_path = hermes_home / "sessions" / f"session_{session_id}.json"
    data = {"messages": messages, **extra}
    core_path.parent.mkdir(parents=True, exist_ok=True)
    core_path.write_text(json.dumps(data), encoding="utf-8")
    return core_path


def _register_active_stream(stream_id):
    """Register stream_id as live in the same state _run_agent_streaming uses."""
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = queue.Queue()


class TestRepairStalePendingNoDeadlock:
    """_repair_stale_pending uses non-blocking lock acquire so callers that
    already hold the per-session lock (retry_last, undo_last, cancel_stream)
    cannot deadlock when get_session() triggers repair on a cache miss."""

    def test_returns_false_when_lock_already_held(self, hermes_home, monkeypatch):
        """If the per-session lock is already held, _repair_stale_pending returns
        False instead of blocking forever (deadlock prevention)."""
        s = _make_stale_session()
        s.save()

        lock = config._get_session_agent_lock(s.session_id)
        # Acquire the lock ourselves — simulating retry_last/undo_last holding it
        assert lock.acquire(blocking=False)

        try:
            result = _repair_stale_pending(s)
            assert result is False, "Should bail out when lock is contended"
        finally:
            lock.release()

    def test_no_deadlock_when_get_session_triggers_repair(self, hermes_home, monkeypatch):
        """Simulate the real deadlock scenario: a caller holds the per-session
        lock and then calls get_session(), which evicts the session from cache
        and re-loads it, triggering _repair_stale_pending.

        Spawns a worker thread that acquires the per-session lock and then calls
        get_session().  The test asserts the worker completes within 5 seconds
        and raises no exception — this reproduces the exact production deadlock
        the prior fix was for.

        When the lock is already held, _repair_stale_pending's non-blocking
        acquire fails, so pending fields are deliberately NOT cleared — this
        preserves safety over repair; the deadlock is avoided."""
        s = _make_stale_session()
        s.save()
        models.SESSIONS[s.session_id] = s

        sid = s.session_id
        completed = threading.Event()
        worker_exc = []

        def _worker():
            lock = config._get_session_agent_lock(sid)
            try:
                with lock:
                    # Evict from cache so get_session re-loads from disk
                    models.SESSIONS.pop(sid, None)
                    # This would deadlock if _repair_stale_pending blocked on the
                    # per-session lock that the caller already holds.
                    result = models.get_session(sid)
                    assert result is not None, "get_session should return a session"
                    # When the lock is held, repair bails (non-blocking acquire
                    # fails) — pending fields are intentionally preserved rather
                    # than risking a deadlock.
                    assert result.pending_user_message is not None, (
                        "Pending fields preserved when lock is held (deadlock prevention)"
                    )
                    assert sid not in models.SESSIONS, (
                        "Still-stale session should not stay pinned in cache after "
                        "lock-contended repair skip"
                    )
            except Exception as exc:
                worker_exc.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        # Worker must finish within 5 seconds — if it doesn't, we deadlocked.
        assert completed.wait(timeout=5), (
            "Worker thread did not complete within 5 seconds — likely deadlock "
            "in get_session() repair path"
        )
        worker.join(timeout=1)

        assert len(worker_exc) == 0, (
            f"Worker raised exception: {worker_exc[0] if worker_exc else 'none'}"
        )

    def test_lock_contended_skip_retries_on_next_cache_miss(self, hermes_home, monkeypatch):
        """A lock-contended repair skip should not become stuck forever.

        The first get_session() call happens while the per-session lock is held,
        so repair must bail to avoid deadlock. The still-stale object is evicted
        from SESSIONS, allowing a later get_session() after lock release to reload
        from disk and repair normally.
        """
        sid = "stale_retry_sid"
        s = _make_stale_session(session_id=sid, pending_msg="Recover me")
        s.save()
        _write_core_transcript(
            hermes_home,
            sid,
            [
                {"role": "user", "content": "Recover me"},
                {"role": "assistant", "content": "Recovered answer"},
            ],
        )
        models.SESSIONS.pop(sid, None)

        lock = config._get_session_agent_lock(sid)
        assert lock.acquire(blocking=False)
        try:
            skipped = models.get_session(sid)
            assert skipped.pending_user_message == "Recover me"
            assert sid not in models.SESSIONS
        finally:
            lock.release()

        repaired = models.get_session(sid)
        assert repaired.pending_user_message is None
        assert repaired.active_stream_id is None
        assert [m["content"] for m in repaired.messages] == ["Recover me", "Recovered answer"]
        assert models.SESSIONS.get(sid) is repaired


class TestDraftRecovery:
    """When no core transcript exists, the pending user message is restored as
    a recovered user turn (_recovered=True) and the error marker says
    'Previous turn did not complete.' — NOT 'preserved as a draft'."""

    def test_pending_message_recovered_as_user_turn(self, hermes_home, monkeypatch):
        """When core transcript is missing, the pending_user_message is appended
        as a user turn with _recovered=True, and its timestamp matches
        pending_started_at when available."""
        _ts = time.time() - 60  # 60 seconds ago
        s = _make_stale_session(pending_msg="My important question")
        s.pending_started_at = _ts
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(s, core_path, stream_id_for_recheck="stream_1")

        assert result is True
        # Find the recovered user turn
        user_msgs = [m for m in s.messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "My important question"
        assert user_msgs[0].get("_recovered") is True
        assert user_msgs[0]["timestamp"] == int(_ts), (
            f"Recovered turn timestamp should match pending_started_at ({_ts}), "
            f"got {user_msgs[0]['timestamp']}"
        )

    def test_error_marker_no_preserved_as_draft(self, hermes_home, monkeypatch):
        """Error marker text must NOT say 'preserved as a draft'."""
        s = _make_stale_session()
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            _apply_core_sync_or_error_marker(s, core_path, stream_id_for_recheck="stream_1")

        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1
        content = error_msgs[0]["content"]
        assert "preserved as a draft" not in content, (
            f"Error marker should not say 'preserved as a draft', got: {content}"
        )
        assert "Previous turn did not complete" in content

    def test_pending_attachments_recovered(self, hermes_home, monkeypatch):
        """Attachments on the pending message are carried over to the recovered turn."""
        s = _make_stale_session()
        s.pending_attachments = [{"type": "image", "name": "photo.png"}]
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            _apply_core_sync_or_error_marker(s, core_path, stream_id_for_recheck="stream_1")

        user_msgs = [m for m in s.messages if m.get("role") == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].get("attachments") == [{"type": "image", "name": "photo.png"}]

    def test_pending_fields_cleared_after_recovery(self, hermes_home, monkeypatch):
        """After recovery, all pending fields are cleared."""
        s = _make_stale_session()
        s.pending_attachments = [{"type": "image", "name": "photo.png"}]
        s.pending_started_at = time.time()
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            _apply_core_sync_or_error_marker(s, core_path, stream_id_for_recheck="stream_1")

        assert s.pending_user_message is None
        assert s.pending_attachments == []
        assert s.pending_started_at is None
        assert s.active_stream_id is None


class TestStreamIdRecheck:
    """Under-lock re-check in _apply_core_sync_or_error_marker bails out when
    active_stream_id has rotated or the stream has come back alive."""

    def test_bails_when_stream_id_rotated(self, hermes_home, monkeypatch):
        """If active_stream_id changed between pre-lock and under-lock check,
        repair bails out (prevents clobbering a new stream's state)."""
        s = _make_stale_session(stream_id="stream_old")
        lock = config._get_session_agent_lock(s.session_id)

        # Simulate the stream ID rotating (e.g. context compression)
        s.active_stream_id = "stream_new"

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_old",
            )

        assert result is False, "Should bail when stream_id rotated"

    def test_bails_when_stream_came_alive(self, hermes_home, monkeypatch):
        """If the stream is alive in STREAMS (cancel not yet processed),
        repair bails out — the streaming thread is still managing the session."""
        s = _make_stale_session(stream_id="stream_alive")
        lock = config._get_session_agent_lock(s.session_id)

        # Register the stream as alive
        _register_active_stream("stream_alive")

        try:
            with lock:
                core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
                result = _apply_core_sync_or_error_marker(
                    s, core_path, stream_id_for_recheck="stream_alive",
                )

            assert result is False, "Should bail when stream is still alive"
        finally:
            with config.STREAMS_LOCK:
                config.STREAMS.pop("stream_alive", None)

    def test_proceeds_when_stream_is_dead(self, hermes_home, monkeypatch):
        """When the stream is not alive (not in STREAMS), repair proceeds."""
        s = _make_stale_session(stream_id="stream_dead")
        lock = config._get_session_agent_lock(s.session_id)

        # Stream is NOT in STREAMS — repair should proceed
        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_dead",
            )

        assert result is True


class TestGetProfileHome:
    """_get_profile_home expands ~ correctly in the ImportError fallback path."""

    def test_expands_tilde_when_profiles_unavailable(self, monkeypatch):
        """When api.profiles import fails, fallback uses HERMES_HOME or ~/.hermes
        with proper tilde expansion."""
        # Make api.profiles import fail
        monkeypatch.setitem(sys.modules, "api.profiles", None)

        # Default fallback without HERMES_HOME env var
        monkeypatch.delenv("HERMES_HOME", raising=False)
        result = _get_profile_home(None)
        assert "~" not in str(result), f"Path should have ~ expanded, got: {result}"
        assert str(result) == str(Path.home() / ".hermes")

    def test_uses_hermes_home_env_var(self, monkeypatch):
        """When HERMES_HOME is set, fallback uses it with expansion."""
        monkeypatch.setitem(sys.modules, "api.profiles", None)
        monkeypatch.setenv("HERMES_HOME", "/custom/hermes")
        result = _get_profile_home(None)
        assert str(result) == "/custom/hermes"

    def test_expands_tilde_in_hermes_home(self, monkeypatch):
        """If HERMES_HOME contains ~, it gets expanded."""
        monkeypatch.setitem(sys.modules, "api.profiles", None)
        monkeypatch.setenv("HERMES_HOME", "~/my-hermes")
        result = _get_profile_home(None)
        assert "~" not in str(result)
        assert str(result) == str(Path.home() / "my-hermes")


class TestCancelInProgressGuard:
    """_last_resort_sync_from_core bails out when a cancel is in progress,
    preventing duplicate markers (cancel_stream already saves partial + cancel marker)."""

    def test_bails_when_cancel_flag_set(self, hermes_home, monkeypatch):
        """If CANCEL_FLAGS[stream_id].is_set(), _last_resort_sync_from_core
        returns immediately without appending any messages."""
        s = _make_stale_session(stream_id="cancel_stream")
        s.save()

        # Set up cancel flag
        cancel_event = threading.Event()
        cancel_event.set()
        config.CANCEL_FLAGS["cancel_stream"] = cancel_event

        # Create an agent lock
        agent_lock = config._get_session_agent_lock(s.session_id)

        # Record message count before
        msg_count_before = len(s.messages)

        streaming._last_resort_sync_from_core(s, "cancel_stream", agent_lock)

        # Should NOT have appended any messages
        assert len(s.messages) == msg_count_before, (
            "Should not append messages when cancel is in progress"
        )
        # Pending fields should NOT have been cleared by _last_resort_sync_from_core
        # (cancel_stream handles that separately)
        assert s.pending_user_message is not None

    def test_proceeds_when_cancel_flag_not_set(self, hermes_home, monkeypatch):
        """When cancel flag is not set, _last_resort_sync_from_core proceeds
        with repair normally."""
        s = _make_stale_session(stream_id="normal_stream")
        s.save()

        # Cancel flag exists but is NOT set
        cancel_event = threading.Event()
        config.CANCEL_FLAGS["normal_stream"] = cancel_event

        agent_lock = config._get_session_agent_lock(s.session_id)
        _register_active_stream("normal_stream")

        streaming._last_resort_sync_from_core(s, "normal_stream", agent_lock)

        # Should have performed repair (appended messages)
        assert len(s.messages) > 0, "Should have appended messages"

    def test_proceeds_when_cancel_flag_absent(self, hermes_home, monkeypatch):
        """When no cancel flag exists for the stream, repair proceeds normally."""
        s = _make_stale_session(stream_id="no_flag_stream")
        s.save()

        # No CANCEL_FLAGS entry at all
        agent_lock = config._get_session_agent_lock(s.session_id)
        _register_active_stream("no_flag_stream")

        streaming._last_resort_sync_from_core(s, "no_flag_stream", agent_lock)

        assert len(s.messages) > 0


class TestEmptyMessagesGuard:
    """_apply_core_sync_or_error_marker bails out when session.messages is
    non-empty, preventing it from clobbering in-memory mutations made by the
    streaming thread or cancel path."""

    def test_pending_cleared_when_messages_nonempty_direct(self, hermes_home, monkeypatch):
        """When _apply_core_sync_or_error_marker is called on a session with
        non-empty messages and pending set, it clears the pending fields and
        appends an error marker, returning True."""
        s = _make_session(messages=[{"role": "user", "content": "hello"}])
        s.pending_user_message = "Another question"
        s.active_stream_id = "stream_1"
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is True
        # Original message should be untouched
        assert len(s.messages) == 1  # no error marker: non-empty messages
        assert s.messages[0]["content"] == "hello"
        # Pending fields cleared
        assert s.pending_user_message is None
        assert s.active_stream_id is None

    def test_bails_when_pending_user_message_none(self, hermes_home, monkeypatch):
        """If pending_user_message is None, repair bails out."""
        s = _make_session(messages=[])
        s.pending_user_message = None
        s.active_stream_id = "stream_1"
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is False

    def test_proceeds_when_messages_empty(self, hermes_home, monkeypatch):
        """When messages is empty and pending_user_message is set, repair proceeds."""
        s = _make_stale_session()
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is True


class TestNonEmptyMessagesPendingCleared:
    """When messages is non-empty and pending is stuck, _last_resort_sync_from_core
    clears the pending fields and appends exactly one error marker without
    clobbering existing messages or syncing from core."""

    def test_pending_cleared_when_messages_nonempty(self, hermes_home, monkeypatch):
        """_last_resort_sync_from_core on a session with both messages and
        pending_user_message clears pending fields and appends exactly one
        error marker."""
        s = _make_session(messages=[{"role": "user", "content": "existing turn"}])
        s.pending_user_message = "Stuck draft"
        s.pending_attachments = [{"type": "image", "name": "screenshot.png"}]
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "stale_stream"
        s.save()

        # Write a core transcript — must NOT be synced because messages is non-empty
        core_messages = [
            {"role": "user", "content": "Core user msg"},
            {"role": "assistant", "content": "Core assistant msg"},
        ]
        _write_core_transcript(hermes_home, s.session_id, core_messages)

        agent_lock = config._get_session_agent_lock(s.session_id)
        _register_active_stream("stale_stream")

        streaming._last_resort_sync_from_core(s, "stale_stream", agent_lock)

        # Existing messages preserved untouched — no error marker appended when
        # messages are non-empty (partial was captured via WAL replay).
        assert len(s.messages) == 1, (
            f"Expected 1 message (original only), got {len(s.messages)}"
        )
        assert s.messages[0]["role"] == "user"
        assert s.messages[0]["content"] == "existing turn"
        assert "Core user msg" not in [m["content"] for m in s.messages], (
            "Core transcript must NOT be synced when messages is non-empty"
        )

        # No error marker when messages are non-empty
        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 0

        # No recovered user turn (messages is non-empty, so skip that)
        recovered_msgs = [m for m in s.messages if m.get("_recovered")]
        assert len(recovered_msgs) == 0

        # Pending fields fully cleared
        assert s.pending_user_message is None
        assert s.pending_attachments == []
        assert s.pending_started_at is None
        assert s.active_stream_id is None


class TestLastResortSyncDelegation:
    """_last_resort_sync_from_core delegates to the shared helpers
    _get_profile_home and _apply_core_sync_or_error_marker, ensuring
    consistent behavior between the streaming exit path and the cache-miss
    repair path."""

    def test_uses_shared_get_profile_home(self, hermes_home, monkeypatch):
        """_last_resort_sync_from_core uses _get_profile_home for path
        resolution, not a local ImportError fallback."""
        s = _make_stale_session()
        s.save()

        agent_lock = config._get_session_agent_lock(s.session_id)

        # Patch _get_profile_home to verify it's called
        called = []
        original_get_profile_home = models._get_profile_home

        def tracking_get_profile_home(profile):
            called.append(profile)
            return original_get_profile_home(profile)

        with patch.object(models, "_get_profile_home", tracking_get_profile_home):
            _register_active_stream("stream_1")
            streaming._last_resort_sync_from_core(s, "stream_1", agent_lock)

        assert len(called) == 1, "_get_profile_home should have been called once"
        assert called[0] == s.profile

    def test_uses_shared_apply_core_sync_or_error_marker(self, hermes_home, monkeypatch):
        """_last_resort_sync_from_core delegates to _apply_core_sync_or_error_marker
        instead of duplicating the logic."""
        s = _make_stale_session()
        s.save()

        agent_lock = config._get_session_agent_lock(s.session_id)

        # Patch _apply_core_sync_or_error_marker to verify it's called
        called = []
        original_fn = models._apply_core_sync_or_error_marker

        def tracking_fn(session, core_path, stream_id_for_recheck=None, **kwargs):
            called.append((session.session_id, stream_id_for_recheck, kwargs))
            return original_fn(session, core_path, stream_id_for_recheck, **kwargs)

        with patch.object(models, "_apply_core_sync_or_error_marker", tracking_fn):
            _register_active_stream("stream_1")
            streaming._last_resort_sync_from_core(s, "stream_1", agent_lock)

        assert len(called) == 1, "_apply_core_sync_or_error_marker should have been called"
        assert called[0][0] == s.session_id
        assert called[0][1] == "stream_1"
        assert called[0][2] == {"require_stream_dead": False}

    def test_core_sync_from_last_resort(self, hermes_home, monkeypatch):
        """When a core transcript exists, _last_resort_sync_from_core syncs
        messages from it (end-to-end test via shared helper)."""
        s = _make_stale_session(pending_msg="My question")
        s.save()

        # Write core transcript with messages
        core_messages = [
            {"role": "user", "content": "My question"},
            {"role": "assistant", "content": "Here is the answer"},
        ]
        _write_core_transcript(hermes_home, s.session_id, core_messages)

        agent_lock = config._get_session_agent_lock(s.session_id)
        _register_active_stream("stream_1")

        streaming._last_resort_sync_from_core(s, "stream_1", agent_lock)

        assert len(s.messages) == 2
        assert s.messages[0]["content"] == "My question"
        assert s.messages[1]["content"] == "Here is the answer"
        assert s.pending_user_message is None
        assert s.active_stream_id is None


class TestCheckpointOrdering:
    """In _run_agent_streaming's outer finally block, checkpoint stop/join
    happens BEFORE _last_resort_sync_from_core. This prevents deadlock because
    the checkpoint thread holds the per-session lock."""

    def test_checkpoint_stops_before_recovery_code_structure(self):
        """Verify the code ordering in the outer finally block of
        _run_agent_streaming: checkpoint stop appears before
        _last_resort_sync_from_core."""
        import inspect
        source = inspect.getsource(streaming._run_agent_streaming)

        # Find the finally block
        finally_idx = source.rfind("finally:")
        assert finally_idx != -1, "Could not find 'finally:' in _run_agent_streaming"

        finally_block = source[finally_idx:]

        # _checkpoint_stop should appear before _last_resort_sync_from_core
        ckpt_pos = finally_block.find("_checkpoint_stop")
        recovery_pos = finally_block.find("_last_resort_sync_from_core")

        assert ckpt_pos != -1, "Could not find _checkpoint_stop in finally block"
        assert recovery_pos != -1, "Could not find _last_resort_sync_from_core in finally block"
        assert ckpt_pos < recovery_pos, (
            f"_checkpoint_stop (pos {ckpt_pos}) must appear BEFORE "
            f"_last_resort_sync_from_core (pos {recovery_pos}) in finally block"
        )


# ── Integration: _repair_stale_pending end-to-end ────────────────────────────

class TestRepairStalePendingIntegration:
    """End-to-end tests for _repair_stale_pending (cache-miss repair path)."""

    def test_repairs_when_core_exists(self, hermes_home, monkeypatch):
        """Full repair path: stale session with core transcript gets synced."""
        s = _make_stale_session()
        s.save()

        core_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        _write_core_transcript(hermes_home, s.session_id, core_messages)

        result = _repair_stale_pending(s)
        assert result is True
        assert len(s.messages) == 2
        assert s.pending_user_message is None

    def test_repairs_when_core_missing(self, hermes_home, monkeypatch):
        """Full repair path: stale session without core gets error marker
        and recovered user turn."""
        s = _make_stale_session(pending_msg="Lost message")
        s.save()

        # No core transcript written
        result = _repair_stale_pending(s)
        assert result is True

        # Should have recovered user turn + error marker
        assert len(s.messages) == 2
        user_msgs = [m for m in s.messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "Lost message"
        assert user_msgs[0].get("_recovered") is True

        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1

    def test_skips_when_messages_nonempty(self, hermes_home, monkeypatch):
        """Pre-check: if messages is non-empty, repair is skipped entirely."""
        s = _make_session(messages=[{"role": "user", "content": "hi"}])
        s.pending_user_message = "more"
        s.active_stream_id = "stream_1"

        result = _repair_stale_pending(s)
        assert result is False

    def test_skips_when_stream_alive(self, hermes_home, monkeypatch):
        """Pre-check: if the stream is still alive in STREAMS, repair is skipped."""
        s = _make_stale_session(stream_id="live_stream")
        s.save()

        _register_active_stream("live_stream")

        try:
            result = _repair_stale_pending(s)
            assert result is False
        finally:
            with config.STREAMS_LOCK:
                config.STREAMS.pop("live_stream", None)

    def test_skips_when_no_pending(self, hermes_home, monkeypatch):
        """Pre-check: if pending_user_message is None, repair is skipped."""
        s = _make_session(messages=[])
        s.pending_user_message = None
        s.active_stream_id = "stream_1"

        result = _repair_stale_pending(s)
        assert result is False


# ── Core sync with metadata fields ───────────────────────────────────────────

class TestCoreSyncMetadata:
    """When syncing from core transcript, token/cost metadata is carried over."""

    def test_syncs_token_and_cost_fields(self, hermes_home, monkeypatch):
        """Core transcript with input_tokens/output_tokens/estimated_cost
        has those fields copied to the session."""
        s = _make_stale_session()
        lock = config._get_session_agent_lock(s.session_id)

        core_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        core_path = _write_core_transcript(
            hermes_home, s.session_id, core_messages,
            input_tokens=100, output_tokens=50, estimated_cost=0.05,
        )

        with lock:
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is True
        assert s.input_tokens == 100
        assert s.output_tokens == 50
        assert s.estimated_cost == 0.05

    def test_core_empty_messages_falls_through_to_recovery(self, hermes_home, monkeypatch):
        """If core transcript exists but messages is empty, the recovery path
        (restoring pending user message + error marker) is taken instead."""
        s = _make_stale_session(pending_msg="My question")
        lock = config._get_session_agent_lock(s.session_id)

        # Core exists but has empty messages
        core_path = _write_core_transcript(hermes_home, s.session_id, [])

        with lock:
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is True
        # Should have recovered user turn + error marker
        user_msgs = [m for m in s.messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "My question"
        assert user_msgs[0].get("_recovered") is True
