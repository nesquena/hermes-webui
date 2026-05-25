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
from api.run_journal import append_run_event


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
    config.ACTIVE_RUNS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.ACTIVE_RUNS.clear()


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


def _register_active_run(stream_id):
    """Register stream_id as an active worker without an attached SSE stream."""
    config.register_active_run(stream_id, session_id="stale_sid", phase="running")


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
    a clear restart interruption marker — NOT 'preserved as a draft'."""

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

    def test_pending_message_recovered_into_context_messages(self, hermes_home, monkeypatch):
        """A recovered pending prompt must remain visible to the next agent turn.

        Sessions that have been auto-compressed feed context_messages to the
        model, not the full display transcript. If stale-stream repair appends
        the recovered user prompt only to messages, the user can see the prompt
        in WebUI but the next agent turn cannot.
        """
        s = _make_session(
            messages=[{"role": "user", "content": "older visible turn"}],
            context_messages=[
                {"role": "user", "content": "older context turn"},
                {"role": "assistant", "content": "older context answer"},
            ],
        )
        s.pending_user_message = "Clip this article https://example.com/post"
        s.active_stream_id = "stream_1"
        lock = config._get_session_agent_lock(s.session_id)

        with lock:
            core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
            result = _apply_core_sync_or_error_marker(
                s, core_path, stream_id_for_recheck="stream_1",
            )

        assert result is True
        assert any(
            m.get("role") == "user"
            and m.get("content") == "Clip this article https://example.com/post"
            and m.get("_recovered") is True
            for m in s.messages
        )
        assert any(
            m.get("role") == "user"
            and m.get("content") == "Clip this article https://example.com/post"
            for m in s.context_messages
        ), "Recovered pending user turn must be included in model context."

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
        assert "Response interrupted" in content
        assert "live response stream stopped" in content
        assert "WebUI process restarted" not in content
        # The marker now arms the lazy-retry hook when a stream id is known
        # ("Recovering the partial output… reload to retry."). The legacy
        # "user message above was preserved" wording is reserved for the
        # no-stream-id repair case; the post-retry-give-up case demotes to
        # the neutral "Partial output may have been lost." wording instead.
        assert (
            "user message above was preserved" in content
            or "Recovering the partial output" in content
        )
        assert error_msgs[0].get("type") == "interrupted"

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
    """_apply_core_sync_or_error_marker preserves existing messages when
    session.messages is non-empty, while still recovering the pending user turn
    before clearing stale stream runtime fields."""

    def test_pending_cleared_when_messages_nonempty_direct(self, hermes_home, monkeypatch):
        """When _apply_core_sync_or_error_marker is called on a session with
        non-empty messages and pending set, it recovers the pending user turn,
        clears the pending fields, and appends an error marker."""
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
        # Original message should be untouched, pending turn recovered, then marker appended
        assert len(s.messages) == 3  # original + recovered user turn + error marker
        assert s.messages[0]["content"] == "hello"
        assert s.messages[1]["role"] == "user"
        assert s.messages[1]["content"] == "Another question"
        assert s.messages[1].get("_recovered") is True
        # Error marker appended
        assert s.messages[2].get("_error") is True
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
    preserves existing messages, recovers the pending user turn, and appends
    exactly one error marker without syncing from core."""

    def test_pending_cleared_when_messages_nonempty(self, hermes_home, monkeypatch):
        """_last_resort_sync_from_core on a session with both messages and
        pending_user_message recovers that pending turn before clearing runtime
        fields and appending exactly one error marker."""
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

        # Existing messages preserved untouched, pending turn recovered, error marker appended
        assert len(s.messages) == 3, (
            f"Expected 3 messages (original + recovered turn + error marker), got {len(s.messages)}"
        )
        assert s.messages[0]["role"] == "user"
        assert s.messages[0]["content"] == "existing turn"
        assert "Core user msg" not in [m["content"] for m in s.messages], (
            "Core transcript must NOT be synced when messages is non-empty"
        )

        # Exactly one recovered user turn
        recovered_msgs = [m for m in s.messages if m.get("_recovered")]
        assert len(recovered_msgs) == 1
        assert recovered_msgs[0]["role"] == "user"
        assert recovered_msgs[0]["content"] == "Stuck draft"
        assert recovered_msgs[0]["attachments"] == [{"type": "image", "name": "screenshot.png"}]

        # Exactly one error marker
        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1
        assert "Response interrupted" in error_msgs[0]["content"]
        assert "live response stream stopped" in error_msgs[0]["content"]
        assert "WebUI process restarted" not in error_msgs[0]["content"]
        assert error_msgs[0].get("type") == "interrupted"

        # Pending fields fully cleared
        assert s.pending_user_message is None
        assert s.pending_attachments == []
        assert s.pending_started_at is None
        assert s.active_stream_id is None

    def test_journaled_partial_output_is_recovered_before_interrupted_marker(self, hermes_home, monkeypatch):
        """When a WebUI restart leaves a dead stream with journaled partial
        output, repair should not collapse the user-visible transcript to only
        a generic interrupted marker."""
        s = _make_session(messages=[{"role": "user", "content": "existing turn"}])
        s.pending_user_message = "Check maintainer activity"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "journaled_stream"
        s.save()

        append_run_event(
            s.session_id,
            "journaled_stream",
            "token",
            {"text": "I will check GitHub first."},
        )
        append_run_event(
            s.session_id,
            "journaled_stream",
            "tool",
            {
                "name": "terminal",
                "preview": "gh pr list --repo nesquena/hermes-webui",
                "args": {"command": "gh pr list --repo nesquena/hermes-webui"},
            },
        )
        append_run_event(
            s.session_id,
            "journaled_stream",
            "tool_complete",
            {"name": "terminal", "duration": 1.2, "is_error": False},
        )
        append_run_event(
            s.session_id,
            "journaled_stream",
            "token",
            {"text": "The first check finished before the restart."},
        )

        core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
        result = _apply_core_sync_or_error_marker(
            s,
            core_path,
            stream_id_for_recheck="journaled_stream",
        )

        assert result is True
        contents = [m.get("content", "") for m in s.messages]
        assert any("I will check GitHub first." in c for c in contents)
        assert any("The first check finished before the restart." in c for c in contents)
        assert s.tool_calls, "journaled tool starts should become visible settled tool cards"
        assert s.tool_calls[0]["name"] == "terminal"
        assert s.tool_calls[0]["done"] is True
        assert s.tool_calls[0]["assistant_msg_idx"] < len(s.messages)
        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1
        assert "partial output above was recovered" in error_msgs[0]["content"]
        assert "no agent output was recovered" not in error_msgs[0]["content"]

    def test_journal_recovery_does_not_materialize_reasoning_only_events(self, hermes_home, monkeypatch):
        """Run-journal repair must not turn hidden reasoning into visible chat
        transcript content."""
        s = _make_session(messages=[{"role": "user", "content": "existing turn"}])
        s.pending_user_message = "Keep going"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "reasoning_only_stream"
        s.save()

        append_run_event(
            s.session_id,
            "reasoning_only_stream",
            "reasoning",
            {"text": "private scratchpad text"},
        )

        core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
        result = _apply_core_sync_or_error_marker(
            s,
            core_path,
            stream_id_for_recheck="reasoning_only_stream",
        )

        assert result is True
        contents = [m.get("content", "") for m in s.messages]
        assert not any("private scratchpad text" in c for c in contents)
        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1
        # Reasoning-only events do not count as visible output, so the marker
        # arms the lazy-retry hook (stream id is known, journal may have more
        # events appear on a later read). The legacy "no agent output was
        # recovered" wording is now reserved for the no-stream-id case.
        assert error_msgs[0].get("_pending_journal_recovery") is True
        assert error_msgs[0].get("_journal_retry_stream_id") == "reasoning_only_stream"
        assert "no agent output was recovered" not in error_msgs[0]["content"]
        assert "Recovering the partial output" in error_msgs[0]["content"]

    def test_journal_recovery_keeps_consecutive_tools_on_one_anchor(self, hermes_home, monkeypatch):
        """Consecutive journaled tools without an intervening visible update
        should recover as one activity group instead of repeated empty anchors."""
        s = _make_session(messages=[{"role": "user", "content": "existing turn"}])
        s.pending_user_message = "Inspect files"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "tool_burst_stream"
        s.save()

        append_run_event(
            s.session_id,
            "tool_burst_stream",
            "token",
            {"text": "I will inspect the relevant files first."},
        )
        for name in ("search_files", "read_file"):
            append_run_event(
                s.session_id,
                "tool_burst_stream",
                "tool",
                {"name": name, "preview": name, "args": {"query": "stream recovery"}},
            )

        core_path = hermes_home / "sessions" / f"session_{s.session_id}.json"
        result = _apply_core_sync_or_error_marker(
            s,
            core_path,
            stream_id_for_recheck="tool_burst_stream",
        )

        assert result is True
        assert len(s.tool_calls) == 2
        assert s.tool_calls[0]["assistant_msg_idx"] == s.tool_calls[1]["assistant_msg_idx"]

    def test_core_sync_branch_recovers_visible_journal_output(self, hermes_home, monkeypatch):
        """The empty-sidecar + populated-core repair branch should still restore
        already-journaled visible output from the interrupted stream."""
        s = _make_session(messages=[])
        s.pending_user_message = "Check maintainer activity"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "core_journal_stream"
        s.save()

        core_messages = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]
        core_path = _write_core_transcript(hermes_home, s.session_id, core_messages)

        append_run_event(
            s.session_id,
            "core_journal_stream",
            "token",
            {"text": "I will check GitHub first."},
        )
        append_run_event(
            s.session_id,
            "core_journal_stream",
            "tool",
            {
                "name": "terminal",
                "preview": "gh pr list --repo nesquena/hermes-webui",
                "args": {"command": "gh pr list --repo nesquena/hermes-webui"},
            },
        )
        append_run_event(
            s.session_id,
            "core_journal_stream",
            "tool_complete",
            {"name": "terminal", "duration": 1.2, "is_error": False},
        )
        append_run_event(
            s.session_id,
            "core_journal_stream",
            "token",
            {"text": "The first check finished before the restart."},
        )

        result = _apply_core_sync_or_error_marker(
            s,
            core_path,
            stream_id_for_recheck="core_journal_stream",
        )

        assert result is True
        contents = [m.get("content", "") for m in s.messages]
        assert contents[:2] == [m["content"] for m in core_messages]
        recovered_users = [m for m in s.messages if m.get("_recovered")]
        assert len(recovered_users) == 1
        assert recovered_users[0]["role"] == "user"
        assert recovered_users[0]["content"] == "Check maintainer activity"
        assert any("I will check GitHub first." in c for c in contents)
        assert any("The first check finished before the restart." in c for c in contents)
        assert s.tool_calls, "journaled tool starts should become visible settled tool cards"
        assert s.tool_calls[0]["name"] == "terminal"
        error_msgs = [m for m in s.messages if m.get("_error")]
        assert len(error_msgs) == 1
        assert "partial output above was recovered" in error_msgs[0]["content"]
        assert s.pending_user_message is None
        assert s.active_stream_id is None

    def test_finished_worker_can_supersede_its_own_interrupted_marker(self):
        """A live worker that finishes after stale repair should be allowed to
        replace the recovery marker for the same user turn."""
        s = _make_session(
            messages=[
                {"role": "user", "content": "deploy"},
                models._interrupted_recovery_marker(),
            ]
        )
        s.active_stream_id = None
        s.pending_user_message = None
        s.pending_attachments = []

        assert streaming._stream_writeback_can_supersede_recovery_marker(s, "deploy")

    def test_finished_worker_does_not_supersede_after_newer_turn_appended(self):
        """Once a follow-up turn changes the visible tail, stale writeback stays
        blocked so old workers cannot overwrite newer transcript state."""
        s = _make_session(
            messages=[
                {"role": "user", "content": "deploy"},
                models._interrupted_recovery_marker(),
                {"role": "user", "content": "what happened?"},
                {"role": "assistant", "content": "I checked the deployment status."},
            ]
        )
        s.active_stream_id = None
        s.pending_user_message = None
        s.pending_attachments = []

        assert not streaming._stream_writeback_can_supersede_recovery_marker(s, "deploy")

    def test_finished_worker_does_not_supersede_different_user_turn(self):
        """The supersede path is tied to the pending prompt that was repaired."""
        s = _make_session(
            messages=[
                {"role": "user", "content": "deploy"},
                models._interrupted_recovery_marker(),
            ]
        )
        s.active_stream_id = None
        s.pending_user_message = None
        s.pending_attachments = []

        assert not streaming._stream_writeback_can_supersede_recovery_marker(s, "ship it")

    def test_core_sync_branch_does_not_duplicate_journal_output_already_in_core(
        self, hermes_home, monkeypatch
    ):
        """If the core transcript already contains the same visible output, the
        journal repair must not append a second copy."""
        s = _make_session(messages=[])
        s.pending_user_message = "Check maintainer activity"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = "duplicate_core_journal_stream"
        s.save()

        core_messages = [
            {"role": "user", "content": "Check maintainer activity"},
            {"role": "assistant", "content": "I will check GitHub first."},
        ]
        core_tool_calls = [
            {
                "name": "terminal",
                "preview": "gh pr list --repo nesquena/hermes-webui",
                "snippet": "gh pr list --repo nesquena/hermes-webui",
                "assistant_msg_idx": 1,
                "done": True,
            },
        ]
        core_path = _write_core_transcript(
            hermes_home,
            s.session_id,
            core_messages,
            tool_calls=core_tool_calls,
        )

        append_run_event(
            s.session_id,
            "duplicate_core_journal_stream",
            "token",
            {"text": "I will check GitHub first."},
        )
        append_run_event(
            s.session_id,
            "duplicate_core_journal_stream",
            "tool",
            {
                "name": "terminal",
                "preview": "gh pr list --repo nesquena/hermes-webui",
                "args": {"command": "gh pr list --repo nesquena/hermes-webui"},
            },
        )

        result = _apply_core_sync_or_error_marker(
            s,
            core_path,
            stream_id_for_recheck="duplicate_core_journal_stream",
        )

        assert result is True
        contents = [m.get("content", "") for m in s.messages]
        assert contents.count("I will check GitHub first.") == 1
        assert len(s.tool_calls) == 1
        assert s.tool_calls[0]["name"] == "terminal"
        assert not [m for m in s.messages if m.get("_error")]


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

    def test_recovers_when_messages_nonempty(self, hermes_home, monkeypatch):
        """Pre-check: if messages is non-empty, repair still preserves the
        pending user turn instead of silently discarding it."""
        s = _make_session(messages=[{"role": "user", "content": "hi"}])
        s.pending_user_message = "more"
        s.active_stream_id = "stream_1"

        result = _repair_stale_pending(s)
        assert result is True
        assert [m["content"] for m in s.messages if m["role"] == "user"] == ["hi", "more"]
        assert s.messages[1].get("_recovered") is True
        assert any(m.get("_error") for m in s.messages)

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

    def test_skips_when_worker_alive_without_sse_stream(self, hermes_home, monkeypatch):
        """Pre-check: if ACTIVE_RUNS still owns the worker, repair is skipped.

        STREAMS is the browser/SSE observation layer and may disappear while the
        backend worker is still running. A detached-but-active worker must not be
        mistaken for a WebUI restart or crashed turn.
        """
        s = _make_stale_session(stream_id="detached_stream")
        s.save()

        _register_active_run("detached_stream")

        assert "detached_stream" in _active_stream_ids()
        result = _repair_stale_pending(s)
        assert result is False
        assert s.pending_user_message == "Hello hermes"
        assert s.active_stream_id == "detached_stream"
        assert s.messages == []

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


# ── Lazy run-journal recovery (read-side self-heal) ─────────────────────────

class TestInterruptedRecoveryMarker:
    """Pure-function tests for _interrupted_recovery_marker(pending_retry=…)."""

    def test_marker_recovered_output_excludes_pending_retry_flag(self):
        marker = models._interrupted_recovery_marker(recovered_output=True)
        assert marker["_error"] is True
        assert marker["type"] == "interrupted"
        assert "_pending_journal_recovery" not in marker
        assert "recovered from the run journal" in marker["content"]

    def test_marker_pending_retry_sets_flag_and_wording(self):
        marker = models._interrupted_recovery_marker(pending_retry=True)
        assert marker.get("_pending_journal_recovery") is True
        assert "Recovering the partial output" in marker["content"]
        assert "no agent output was recovered" not in marker["content"]

    def test_marker_recovered_output_beats_pending_retry(self):
        marker = models._interrupted_recovery_marker(
            recovered_output=True, pending_retry=True,
        )
        assert "_pending_journal_recovery" not in marker
        assert "recovered from the run journal" in marker["content"]

    def test_marker_default_wording_unchanged_for_no_output_no_retry(self):
        marker = models._interrupted_recovery_marker()
        assert "_pending_journal_recovery" not in marker
        assert "no agent output was recovered" in marker["content"]


class TestRetryJournalRecoveryInPlace:
    """In-place retry helper: promote / increment / give up."""

    def _make_pending_session(self, hermes_home, stream_id="lazy_stream",
                              attempts=0, first_seen_ts=None):
        s = _make_session(messages=[
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": "Stuck draft", "_recovered": True},
        ])
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = stream_id
        marker["_journal_retry_attempts"] = attempts
        marker["_journal_retry_first_seen_ts"] = (
            first_seen_ts if first_seen_ts is not None else int(time.time())
        )
        s.messages.append(marker)
        s.save()
        return s

    def test_promotes_marker_when_journal_now_available(self, hermes_home, monkeypatch):
        stream_id = "lazy_stream_promote"
        s = self._make_pending_session(hermes_home, stream_id=stream_id)
        marker_before = s.messages[-1]

        append_run_event(s.session_id, stream_id, "token", {"text": "Late tokens."})
        append_run_event(
            s.session_id, stream_id, "tool",
            {"name": "terminal", "preview": "echo", "args": {"cmd": "echo"}},
        )

        ok = models._retry_journal_recovery_in_place(s)
        assert ok is True

        # Marker promoted in place and meta stripped
        marker_idx = next(
            i for i, m in enumerate(s.messages)
            if m.get("type") == "interrupted" and m.get("_error")
        )
        promoted = s.messages[marker_idx]
        assert promoted is marker_before
        assert "recovered from the run journal" in promoted["content"]
        assert "_pending_journal_recovery" not in promoted
        assert "_journal_retry_stream_id" not in promoted
        assert "_journal_retry_attempts" not in promoted
        assert "_journal_retry_first_seen_ts" not in promoted

        # Journaled rows reordered ABOVE the marker, preserving order
        before_marker = s.messages[:marker_idx]
        recovered = [m for m in before_marker if m.get("_recovered_from_run_journal")]
        assert recovered, "journaled assistant rows must sit above the marker"
        assert any("Late tokens." in m.get("content", "") for m in recovered)
        # tool_call.assistant_msg_idx still points into the valid range
        for tc in s.tool_calls or []:
            idx = tc.get("assistant_msg_idx")
            if isinstance(idx, int):
                assert 0 <= idx < len(s.messages)

    def test_increments_attempts_when_journal_still_empty(self, hermes_home, monkeypatch):
        stream_id = "lazy_stream_increment"
        s = self._make_pending_session(hermes_home, stream_id=stream_id)
        # No append_run_event — journal stays empty.
        ok = models._retry_journal_recovery_in_place(s)
        assert ok is False
        marker = s.messages[-1]
        assert marker.get("_pending_journal_recovery") is True
        assert marker.get("_journal_retry_attempts") == 1
        assert "Recovering the partial output" in marker["content"]

    def test_demotes_to_neutral_after_max_attempts(self, hermes_home, monkeypatch):
        stream_id = "lazy_stream_giveup_attempts"
        s = self._make_pending_session(
            hermes_home, stream_id=stream_id,
            attempts=models._JOURNAL_RETRY_MAX_ATTEMPTS,
        )
        ok = models._retry_journal_recovery_in_place(s)
        assert ok is False
        marker = s.messages[-1]
        assert "_pending_journal_recovery" not in marker
        assert "_journal_retry_stream_id" not in marker
        assert "_journal_retry_attempts" not in marker
        assert "_journal_retry_first_seen_ts" not in marker
        assert "Partial output may have been lost" in marker["content"]
        assert "Recovering the partial output" not in marker["content"]

    def test_demotes_to_neutral_after_giveup_seconds(self, hermes_home, monkeypatch):
        stream_id = "lazy_stream_giveup_age"
        first_seen = int(time.time()) - (models._JOURNAL_RETRY_GIVEUP_SECONDS + 60)
        s = self._make_pending_session(
            hermes_home, stream_id=stream_id, first_seen_ts=first_seen,
        )
        ok = models._retry_journal_recovery_in_place(s)
        assert ok is False
        marker = s.messages[-1]
        assert "_pending_journal_recovery" not in marker
        assert "Partial output may have been lost" in marker["content"]

    def test_noop_when_no_pending_marker(self, hermes_home, monkeypatch):
        s = _make_session(messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ])
        s.save()
        spy = []
        original = models._append_journaled_partial_output

        def _spy(*a, **kw):
            spy.append(1)
            return original(*a, **kw)

        monkeypatch.setattr(models, "_append_journaled_partial_output", _spy)
        ok = models._retry_journal_recovery_in_place(s)
        assert ok is False
        assert spy == [], "no pending marker → must not call recovery"

    def test_short_circuit_helper_detects_pending_marker(self, hermes_home, monkeypatch):
        s = _make_session(messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ])
        assert models._session_has_pending_journal_retry(s) is False
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = "abc"
        marker["_journal_retry_attempts"] = 0
        marker["_journal_retry_first_seen_ts"] = int(time.time())
        s.messages.append(marker)
        assert models._session_has_pending_journal_retry(s) is True

    def test_short_circuit_helper_stops_at_normal_assistant(self, hermes_home, monkeypatch):
        s = _make_session(messages=[
            {"role": "user", "content": "x"},
        ])
        # An old, already-promoted marker followed by a normal assistant turn —
        # the pending flag belongs to a prior turn that was healed; helper must
        # not loop back into it.
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = "abc"
        marker["_journal_retry_attempts"] = 0
        marker["_journal_retry_first_seen_ts"] = int(time.time())
        s.messages.append(marker)
        s.messages.append({"role": "user", "content": "later"})
        s.messages.append({"role": "assistant", "content": "later reply"})
        assert models._session_has_pending_journal_retry(s) is False


class TestGetSessionLazyRetryHook:
    """get_session() must trigger _retry_journal_recovery_in_place on both
    cache-hit and cold-load paths when a pending marker exists, and skip
    quickly when nothing is pending."""

    def _make_session_with_pending_marker(self, sid="lazy_get", stream_id="st"):
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "u2", "_recovered": True},
        ])
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = stream_id
        marker["_journal_retry_attempts"] = 0
        marker["_journal_retry_first_seen_ts"] = int(time.time())
        s.messages.append(marker)
        return s

    def test_triggers_retry_on_cache_hit(self, hermes_home, monkeypatch):
        sid = "lazy_get_cache"
        stream_id = "stream_cache"
        s = self._make_session_with_pending_marker(sid=sid, stream_id=stream_id)
        s.save()
        models.SESSIONS[sid] = s
        append_run_event(sid, stream_id, "token", {"text": "Late."})

        reloaded = models.get_session(sid)
        assert reloaded is s
        marker = s.messages[-1] if "Recovering" in s.messages[-1]["content"] else next(
            m for m in s.messages
            if m.get("type") == "interrupted" and m.get("_error")
        )
        assert "recovered from the run journal" in marker["content"]
        assert "_pending_journal_recovery" not in marker

    def test_triggers_retry_on_cold_load(self, hermes_home, monkeypatch):
        sid = "lazy_get_cold"
        stream_id = "stream_cold"
        s = self._make_session_with_pending_marker(sid=sid, stream_id=stream_id)
        s.save()
        models.SESSIONS.pop(sid, None)
        append_run_event(sid, stream_id, "token", {"text": "Late."})

        reloaded = models.get_session(sid)
        marker = next(
            m for m in reloaded.messages
            if m.get("type") == "interrupted" and m.get("_error")
        )
        assert "recovered from the run journal" in marker["content"]
        assert "_pending_journal_recovery" not in marker

    def test_short_circuit_when_no_pending_marker(self, hermes_home, monkeypatch):
        sid = "lazy_get_no_pending"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ])
        s.save()
        models.SESSIONS[sid] = s
        spy = []
        monkeypatch.setattr(
            models, "_retry_journal_recovery_in_place",
            lambda session: spy.append(1) or False,
        )
        models.get_session(sid)
        assert spy == []

    def test_metadata_only_skips_retry(self, hermes_home, monkeypatch):
        sid = "lazy_get_meta"
        stream_id = "stream_meta"
        s = self._make_session_with_pending_marker(sid=sid, stream_id=stream_id)
        s.save()
        models.SESSIONS[sid] = s
        spy = []
        monkeypatch.setattr(
            models, "_retry_journal_recovery_in_place",
            lambda session: spy.append(1) or False,
        )
        models.get_session(sid, metadata_only=True)
        assert spy == [], "metadata_only must skip the lazy-retry helper"


class TestLazyRetryBackwardsCompat:
    """Pre-fix session shapes must continue to work."""

    def test_legacy_marker_without_flag_unchanged(self, hermes_home, monkeypatch):
        """An old session whose marker carries the legacy 'no agent output'
        wording (no flag) must not be touched by get_session()."""
        sid = "legacy_marker_sid"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "later", "_recovered": True},
        ])
        legacy = models._interrupted_recovery_marker(recovered_output=False)
        s.messages.append(legacy)
        s.save()
        models.SESSIONS.pop(sid, None)
        spy = []
        original = models._append_journaled_partial_output
        monkeypatch.setattr(
            models, "_append_journaled_partial_output",
            lambda *a, **kw: spy.append(1) or original(*a, **kw),
        )
        models.get_session(sid)
        assert spy == [], "legacy marker (no flag) must not re-trigger recovery"

    def test_pending_retry_marker_round_trips_through_session_save_and_load(
            self, hermes_home, monkeypatch):
        """All four retry meta keys must survive Session.save() / Session.load()."""
        sid = "round_trip_sid"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
        ])
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = "abc"
        marker["_journal_retry_attempts"] = 3
        marker["_journal_retry_first_seen_ts"] = 1779200000
        s.messages.append(marker)
        s.save()
        models.SESSIONS.pop(sid, None)

        reloaded = Session.load(sid)
        last = reloaded.messages[-1]
        assert last.get("_pending_journal_recovery") is True
        assert last.get("_journal_retry_stream_id") == "abc"
        assert last.get("_journal_retry_attempts") == 3
        assert last.get("_journal_retry_first_seen_ts") == 1779200000


class TestJournalToolDedupeScoping:
    """`_journal_tool_already_present` must only collapse against tool cards
    recovered from the same stream — a repeated tool (e.g. ``terminal: ls``)
    in a previous turn must NOT pre-empt this turn's recovery."""

    def test_repeated_tool_in_earlier_turn_does_not_block_recovery(self, hermes_home, monkeypatch):
        sid = "dedupe_scope_sid"
        stream_id = "dedupe_scope_stream"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": "later turn", "_recovered": True},
        ])
        # Pre-existing tool card from an earlier turn with same (name, preview)
        # but a different recovered-stream-id. The retry must NOT see this as a
        # hit when dedupe_existing is asked to scope to ``stream_id``.
        s.tool_calls = [
            {
                "name": "terminal",
                "preview": "ls",
                "snippet": "ls",
                "tid": "old-1",
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": "earlier_stream",
                "done": True,
            }
        ]
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = stream_id
        marker["_journal_retry_attempts"] = 0
        marker["_journal_retry_first_seen_ts"] = int(time.time())
        s.messages.append(marker)
        s.save()
        append_run_event(sid, stream_id, "token", {"text": "Listing."})
        append_run_event(
            sid, stream_id, "tool",
            {"name": "terminal", "preview": "ls", "args": {"cmd": "ls"}},
        )

        ok = models._retry_journal_recovery_in_place(s)
        assert ok is True

        # Two tool cards now: the old one untouched, plus a new one for this
        # stream. If dedupe were session-wide the new one would be dropped.
        scoped = [
            tc for tc in s.tool_calls
            if tc.get("_recovered_stream_id") == stream_id
        ]
        assert len(scoped) == 1
        assert scoped[0]["name"] == "terminal"
        assert scoped[0]["preview"] == "ls"
        # Old tool card is preserved.
        assert any(
            tc.get("_recovered_stream_id") == "earlier_stream"
            for tc in s.tool_calls
        )

    def test_untagged_tool_still_matches_for_core_transcript_invariant(self, hermes_home, monkeypatch):
        """Tool cards without ``_recovered_stream_id`` (live tools, or tools
        carried over from the core transcript) match regardless of the
        ``stream_id`` argument. This preserves the "core transcript already
        contains this tool, don't duplicate it" invariant the original repair
        path relies on."""
        s = _make_session(messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ])
        s.tool_calls = [
            {"name": "terminal", "preview": "ls", "snippet": "ls"},
        ]
        # No stream_id → legacy session-wide check returns True.
        assert models._journal_tool_already_present(s, "terminal", "ls") is True
        # With a stream_id, untagged tool cards still match (different stream
        # ids only override the match decision when the existing card itself
        # is tagged with a stream id that disagrees).
        assert models._journal_tool_already_present(
            s, "terminal", "ls", stream_id="some_stream",
        ) is True

    def test_tagged_tool_with_different_stream_does_not_match(self, hermes_home, monkeypatch):
        """A tool card tagged with a different recovered_stream_id must NOT
        be considered a duplicate when the retry is scoped to a different
        stream."""
        s = _make_session(messages=[
            {"role": "user", "content": "x"},
        ])
        s.tool_calls = [
            {
                "name": "terminal",
                "preview": "ls",
                "snippet": "ls",
                "_recovered_stream_id": "other_stream",
            },
        ]
        assert models._journal_tool_already_present(
            s, "terminal", "ls", stream_id="this_stream",
        ) is False
        # But scoping to the same stream id matches.
        assert models._journal_tool_already_present(
            s, "terminal", "ls", stream_id="other_stream",
        ) is True


class TestWslPageCacheRace:
    """Cover the WSL2 / network-FS shape: read_run_events returns empty / errors
    first, recovers on a later call."""

    def test_first_read_raises_oserror_second_read_succeeds(self, hermes_home, monkeypatch):
        sid = "wsl_race_sid"
        stream_id = "wsl_race_stream"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
        ])
        s.pending_user_message = "Keep going"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = stream_id

        # Simulate first read raising IOError, then succeeding.
        import api.run_journal as run_journal
        real = run_journal.read_run_events
        attempts = {"n": 0}

        def flaky_read(sid_, run_id, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("EIO simulated by test")
            return real(sid_, run_id, **kw)

        monkeypatch.setattr(run_journal, "read_run_events", flaky_read)

        core_path = hermes_home / "sessions" / f"session_{sid}.json"
        result = _apply_core_sync_or_error_marker(
            s, core_path, stream_id_for_recheck=stream_id,
        )
        assert result is True
        marker = next(m for m in s.messages if m.get("type") == "interrupted")
        assert marker.get("_pending_journal_recovery") is True

        # Now write journal events; the next retry call will read them.
        append_run_event(sid, stream_id, "token", {"text": "Came back."})
        ok = models._retry_journal_recovery_in_place(s)
        assert ok is True
        marker_after = next(m for m in s.messages if m.get("type") == "interrupted")
        assert "recovered from the run journal" in marker_after["content"]
        assert "_pending_journal_recovery" not in marker_after

    def test_journal_grows_between_reads(self, hermes_home, monkeypatch):
        sid = "wsl_grow_sid"
        stream_id = "wsl_grow_stream"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
        ])
        s.pending_user_message = "Keep going"
        s.pending_started_at = time.time() - 120
        s.active_stream_id = stream_id

        # First repair pass: nothing visible yet.
        core_path = hermes_home / "sessions" / f"session_{sid}.json"
        result = _apply_core_sync_or_error_marker(
            s, core_path, stream_id_for_recheck=stream_id,
        )
        assert result is True
        marker = next(m for m in s.messages if m.get("type") == "interrupted")
        assert marker.get("_pending_journal_recovery") is True

        # Journal grows.
        append_run_event(sid, stream_id, "token", {"text": "Partial 1."})
        append_run_event(sid, stream_id, "token", {"text": " Partial 2."})
        append_run_event(
            sid, stream_id, "tool",
            {"name": "terminal", "preview": "ls", "args": {"cmd": "ls"}},
        )

        ok = models._retry_journal_recovery_in_place(s)
        assert ok is True
        marker_after = next(m for m in s.messages if m.get("type") == "interrupted")
        assert "recovered from the run journal" in marker_after["content"]
        # Both tokens recovered, in order, before the marker.
        marker_idx = s.messages.index(marker_after)
        recovered_text = " ".join(
            m.get("content", "") for m in s.messages[:marker_idx]
            if m.get("_recovered_from_run_journal")
        )
        assert "Partial 1." in recovered_text and "Partial 2." in recovered_text

    def test_concurrent_get_session_calls_idempotent(self, hermes_home, monkeypatch):
        sid = "wsl_concurrent_sid"
        stream_id = "wsl_concurrent_stream"
        s = _make_session(session_id=sid, messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "later", "_recovered": True},
        ])
        marker = models._interrupted_recovery_marker(pending_retry=True)
        marker["_journal_retry_stream_id"] = stream_id
        marker["_journal_retry_attempts"] = 0
        marker["_journal_retry_first_seen_ts"] = int(time.time())
        s.messages.append(marker)
        s.save()
        append_run_event(sid, stream_id, "token", {"text": "ConcurrentTokens"})
        models.SESSIONS[sid] = s

        results = []

        def _worker():
            try:
                models.get_session(sid)
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert results.count("ok") == 2, f"both workers must succeed: {results}"

        # Exactly one interrupted marker remains, exactly one journal-recovered
        # body (deduped by dedupe_existing=True).
        interrupted_markers = [m for m in s.messages if m.get("type") == "interrupted"]
        assert len(interrupted_markers) == 1
        recovered = [m for m in s.messages if m.get("_recovered_from_run_journal")]
        assert sum(1 for m in recovered if "ConcurrentTokens" in m.get("content", "")) == 1
