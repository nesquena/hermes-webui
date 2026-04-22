"""
Tests for periodic session persistence during streaming (Issue #765).

Validates:
  - Session.save(skip_index=True) writes the JSON file but skips the index rebuild
  - The periodic checkpoint fires when _checkpoint_activity is incremented
    (as it would be by on_tool() during real agent execution)
  - Messages stored via pending_user_message survive a simulated server restart
"""
import json
import threading
import time
from pathlib import Path

import pytest

import api.models as models
from api.models import Session


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


def _make_session(session_id="abc123", messages=None):
    """Helper to create a Session with a known ID."""
    return Session(
        session_id=session_id,
        title="Test Session",
        messages=messages or [{"role": "user", "content": "hello"}],
    )


class TestSaveSkipIndex:
    """Tests for the skip_index parameter on Session.save()."""

    def test_save_writes_json_file(self):
        """save() always writes the session JSON file, regardless of skip_index."""
        s = _make_session("s1")
        s.save()
        assert s.path.exists()
        data = json.loads(s.path.read_text())
        assert data["session_id"] == "s1"
        assert len(data["messages"]) == 1

    def test_save_with_skip_index_writes_json(self):
        """save(skip_index=True) still writes the session JSON file."""
        s = _make_session("s2")
        s.save(skip_index=True)
        assert s.path.exists()
        data = json.loads(s.path.read_text())
        assert data["session_id"] == "s2"

    def test_save_with_skip_index_skips_index_rebuild(self):
        """save(skip_index=True) does NOT create or update the session index."""
        s = _make_session("s3")
        s.save(skip_index=True)
        index = models.SESSION_INDEX_FILE
        assert not index.exists(), "Index file should not be created with skip_index=True"

    def test_save_without_skip_index_creates_index(self):
        """save() (default) DOES create the session index."""
        s = _make_session("s4")
        s.save()
        index = models.SESSION_INDEX_FILE
        assert index.exists(), "Index file should be created by default save()"
        data = json.loads(index.read_text())
        sids = [e["session_id"] for e in data]
        assert "s4" in sids

    def test_skip_index_then_full_save_updates_index(self):
        """After skip_index saves, a full save() correctly builds the index."""
        s = _make_session("s5")
        s.messages.append({"role": "assistant", "content": "hi there"})
        s.save(skip_index=True)
        assert not models.SESSION_INDEX_FILE.exists()

        s.messages.append({"role": "user", "content": "thanks"})
        s.save()
        assert models.SESSION_INDEX_FILE.exists()
        data = json.loads(s.path.read_text())
        assert len(data["messages"]) == 3

    def test_skip_index_save_with_touch_updated_at_false(self):
        """save(skip_index=True, touch_updated_at=False) preserves updated_at."""
        s = _make_session("touch1")
        original_updated_at = s.updated_at
        time.sleep(0.05)
        s.save(skip_index=True, touch_updated_at=False)
        data = json.loads(s.path.read_text())
        assert data["updated_at"] == original_updated_at
        assert not models.SESSION_INDEX_FILE.exists()


class TestPeriodicCheckpoint:
    """Tests for the periodic checkpoint mechanism during streaming.

    The checkpoint is keyed off an activity counter (_checkpoint_activity[0]),
    incremented by on_tool() on each tool.completed event — NOT off s.messages
    which is never mutated during agent.run_conversation() (the agent copies it).
    """

    def test_checkpoint_fires_on_activity_counter_increment(self):
        """Checkpoint saves when _checkpoint_activity counter grows."""
        s = _make_session("ckpt1")
        s.pending_user_message = "do a long task"
        s.save()  # initial save (like routes.py does before streaming starts)

        stop_event = threading.Event()
        _checkpoint_activity = [0]
        save_count = [0]

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.1):  # fast interval for test
                try:
                    cur = _checkpoint_activity[0]
                    if cur > last:
                        s.save(skip_index=True)
                        last = cur
                        save_count[0] += 1
                except Exception:
                    pass

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()

        # Simulate on_tool() completing twice (as would happen during a real agent run)
        time.sleep(0.15)
        _checkpoint_activity[0] += 1  # first tool completes
        time.sleep(0.25)
        _checkpoint_activity[0] += 1  # second tool completes
        time.sleep(0.25)

        stop_event.set()
        t.join(timeout=2)

        assert save_count[0] >= 2, (
            "Expected at least 2 checkpoint saves (one per activity increment); "
            f"got {save_count[0]}"
        )
        # Verify the JSON is on disk and readable
        data = json.loads(s.path.read_text())
        assert data["pending_user_message"] == "do a long task"

    def test_checkpoint_does_not_fire_without_activity(self):
        """Checkpoint skips save when activity counter has not changed."""
        s = _make_session("ckpt2")
        s.save()

        stop_event = threading.Event()
        _checkpoint_activity = [0]
        save_count = [0]

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.05):
                cur = _checkpoint_activity[0]
                if cur > last:
                    s.save(skip_index=True)
                    last = cur
                    save_count[0] += 1

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        # No increments — checkpoint should stay quiet
        time.sleep(0.4)
        stop_event.set()
        t.join(timeout=2)

        assert save_count[0] == 0, (
            f"Expected 0 saves when activity is unchanged; got {save_count[0]}"
        )

    def test_checkpoint_stops_on_signal(self):
        """Checkpoint thread exits cleanly when stop event is set."""
        s = _make_session("ckpt3")
        stop_event = threading.Event()
        iterations = [0]

        def periodic_checkpoint():
            while not stop_event.wait(0.02):
                iterations[0] += 1

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.15)
        stop_event.set()
        t.join(timeout=1)
        assert not t.is_alive(), "Checkpoint thread should have stopped"

    def test_pending_message_survives_simulated_restart(self):
        """pending_user_message written before run_conversation survives a restart.

        This is the minimal guarantee for Issue #765: even if the agent produces
        no tool calls before a crash, the user's message is not silently lost.
        """
        s = _make_session("survive1", messages=[{"role": "user", "content": "first turn"}])
        s.save()  # initial full save

        # Simulate what routes.py does before _run_agent_streaming:
        s.pending_user_message = "do a long research task"
        s.pending_started_at = time.time()
        s.active_stream_id = "stream-abc123"
        s.save(skip_index=True)  # checkpoint-style save

        # Simulate restart: clear in-memory state, reload from disk
        del s
        models.SESSIONS.clear()

        reloaded = Session.load("survive1")
        assert reloaded is not None
        assert reloaded.pending_user_message == "do a long research task"
        assert reloaded.active_stream_id == "stream-abc123"
        # Original messages still intact
        assert len(reloaded.messages) == 1

    def test_activity_checkpoint_persists_updated_at(self):
        """Each checkpoint save updates updated_at, keeping session fresh in sidebar."""
        s = _make_session("ts1")
        s.save()
        ts_before = s.updated_at

        time.sleep(0.05)
        _checkpoint_activity = [1]  # simulate one tool completion

        stop_event = threading.Event()

        def periodic_checkpoint():
            last = 0
            while not stop_event.wait(0.05):
                cur = _checkpoint_activity[0]
                if cur > last:
                    s.save(skip_index=True)
                    last = cur

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=1)

        data = json.loads(s.path.read_text())
        assert data["updated_at"] > ts_before, "Checkpoint should update updated_at"


class TestCheckpointVariableLifecycle:
    """Regression guard: the outer `finally` must not UnboundLocalError when an
    exception fires before the checkpoint thread is created.  _checkpoint_stop
    is initialised to None at the very top of the outer try block so the
    finally's `if _checkpoint_stop is not None` branch is always safe.
    """


class TestIssue765FollowupHardening:
    """Regression tests for the follow-up hardening pass on Issue #765."""

    def test_same_session_concurrent_saves_use_distinct_temp_files(self, monkeypatch):
        """Two concurrent saves of the same session must not collide on one tmp path.

        The key regression guard here is that each save call should reach os.replace()
        with a distinct source tmp path. With the old shared `<sid>.tmp` scheme, both
        threads would target the same path and the second replace would deterministically
        fail once the first consume/remove happened.
        """
        s = _make_session("same_sid")
        s.save(skip_index=True)  # seed the file on disk

        original_replace = models.os.replace
        barrier = threading.Barrier(2)
        replace_sources = []
        errors = []

        def _replace_with_barrier(src, dst):
            replace_sources.append(str(src))
            barrier.wait(timeout=5)
            return original_replace(src, dst)

        monkeypatch.setattr(models.os, "replace", _replace_with_barrier)

        def _save_worker():
            try:
                s.save(skip_index=True)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_save_worker)
        t2 = threading.Thread(target=_save_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Concurrent same-session saves should not fail: {errors}"
        assert len(replace_sources) == 2, f"Expected 2 replace calls, got {replace_sources}"
        assert len(set(replace_sources)) == 2, (
            "Concurrent same-session saves must use distinct temp files; "
            f"got {replace_sources}"
        )
        data = json.loads(s.path.read_text(encoding="utf-8"))
        assert data["session_id"] == "same_sid"

    def test_success_path_joins_checkpoint_before_session_mutation(self):
        """Static guard: success path must stop/join checkpoint thread before mutating.

        This keeps the post-run_conversation session rewrite serialized relative to the
        periodic checkpoint worker.
        """
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(
            encoding="utf-8"
        )
        stop_idx = src.find("if _checkpoint_stop is not None:\n                _checkpoint_stop.set()")
        join_idx = src.find("if _ckpt_thread is not None:\n                _ckpt_thread.join(timeout=15)")
        lock_idx = src.find("with _agent_lock:\n                s.messages = _restore_reasoning_metadata(")
        save_idx = src.find("s.messages = _restore_reasoning_metadata(")

        assert stop_idx != -1, "Success path must stop the checkpoint thread"
        assert join_idx != -1, "Success path must join the checkpoint thread"
        assert lock_idx != -1, "Success path must serialize mutation with _agent_lock"
        assert save_idx != -1, "Success path restore/mutation block not found"
        assert stop_idx < join_idx < lock_idx <= save_idx, (
            "Checkpoint stop/join must happen before the success-path session mutation block"
        )

    def test_silent_failure_path_does_not_reacquire_agent_lock(self):
        """Silent-failure path must not nest `_agent_lock` inside the success lock.

        Reacquiring the same per-session lock inside the post-run_conversation block
        deadlocks because `_get_session_agent_lock()` returns a non-reentrant Lock.
        """
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(
            encoding="utf-8"
        )
        outer_lock_idx = src.find("with _agent_lock:\n                s.messages = _restore_reasoning_metadata(")
        silent_failure_idx = src.find("if not _assistant_added and not _token_sent:")
        inner_lock_idx = src.find("with _agent_lock:", outer_lock_idx + 1)
        compression_idx = src.find("# ── Handle context compression side effects ──")

        assert outer_lock_idx != -1, "Outer success-path _agent_lock block not found"
        assert silent_failure_idx != -1, "Silent-failure branch not found"
        assert compression_idx != -1, "Compression marker not found"
        assert not (
            inner_lock_idx != -1 and silent_failure_idx < inner_lock_idx < compression_idx
        ), "Silent-failure path must not reacquire _agent_lock inside the outer lock"

    def test_checkpoint_stop_initialised_before_any_raiseable_code(self):
        """Static check: `_checkpoint_stop = None` must appear before any code
        that could raise inside _run_agent_streaming's outer try."""
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(
            encoding="utf-8"
        )
        lines = src.splitlines()
        try_line = next(
            i for i, ln in enumerate(lines, 1)
            if ln.rstrip().endswith("try:")
            and any(
                lines[j].strip().startswith("_checkpoint_stop = None")
                for j in range(max(0, i - 4), i - 1)
            )
        )
        # The assignment must precede the `try:` — not sit inside the nested
        # block where an earlier line could raise before it runs.
        init_line = next(
            i for i, ln in enumerate(lines, 1)
            if "_checkpoint_stop = None" in ln
        )
        assert init_line < try_line, (
            f"_checkpoint_stop = None (line {init_line}) must precede the outer "
            f"try block (line {try_line}) so the finally can safely check it."
        )

    def test_finally_path_when_early_exception_does_not_unbound_error(self):
        """Mirror the _run_agent_streaming try/finally structure — proves that
        pre-initialising _checkpoint_stop = None outside any raiseable code
        keeps the finally safe."""

        def mimic_run_agent_streaming():
            _checkpoint_stop = None  # pre-init (the fix)
            try:
                # Anything here could raise — simulate early failure
                raise ValueError("early failure, e.g. get_session KeyError")
                _checkpoint_stop = threading.Event()  # never reached
            finally:
                # The guard the PR added — must not itself raise
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()

        with pytest.raises(ValueError, match="early failure"):
            mimic_run_agent_streaming()
