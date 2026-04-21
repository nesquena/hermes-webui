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
