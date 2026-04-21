"""
Tests for periodic session persistence during streaming (Issue #765).

Validates:
  - Session.save(skip_index=True) writes the JSON file but skips the index rebuild
  - The periodic checkpoint timer fires during a simulated long-running task
  - Messages accumulated during streaming are persisted to disk before completion
"""
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

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

    def test_save_writes_json_file(self, tmp_path):
        """save() always writes the session JSON file, regardless of skip_index."""
        s = _make_session("s1")
        s.save()
        assert (s.path).exists()
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
        # Index should NOT exist after a skip_index save
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
        # First save: skip index (like periodic checkpoint)
        s.messages.append({"role": "assistant", "content": "hi there"})
        s.save(skip_index=True)
        assert not models.SESSION_INDEX_FILE.exists()

        # Second save: full persist (like final save on task completion)
        s.messages.append({"role": "user", "content": "thanks"})
        s.save()
        assert models.SESSION_INDEX_FILE.exists()
        data = json.loads(s.path.read_text())
        assert len(data["messages"]) == 3  # user + assistant + user


class TestPeriodicCheckpoint:
    """Tests for the periodic checkpoint mechanism during streaming."""

    def test_checkpoint_timer_persists_new_messages(self):
        """The periodic checkpoint timer saves messages as they accumulate."""
        s = _make_session("ckpt1", messages=[{"role": "user", "content": "hello"}])

        stop_event = threading.Event()
        msg_count = [len(s.messages)]

        def periodic_checkpoint():
            """Mimics the _periodic_checkpoint closure from streaming.py."""
            while not stop_event.wait(0.2):  # 200ms interval for fast test
                try:
                    cur = len(s.messages)
                    if cur > msg_count[0]:
                        s.save(skip_index=True)
                        msg_count[0] = cur
                except Exception:
                    pass

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()

        # Simulate agent adding messages over time
        time.sleep(0.3)  # Let timer fire once
        s.messages.append({"role": "assistant", "content": "partial response 1"})
        time.sleep(0.3)  # Let timer fire again
        s.messages.append({"role": "assistant", "content": "partial response 2"})
        time.sleep(0.3)  # Let timer fire again

        stop_event.set()
        t.join(timeout=2)

        # Verify: the session file on disk has all messages
        data = json.loads(s.path.read_text())
        assert len(data["messages"]) == 3  # user + 2 assistant messages
        assert data["messages"][1]["content"] == "partial response 1"
        assert data["messages"][2]["content"] == "partial response 2"

    def test_checkpoint_timer_does_not_save_unchanged_session(self):
        """The periodic checkpoint skips save when no new messages were added."""
        s = _make_session("ckpt2", messages=[{"role": "user", "content": "hello"}])
        s.save()  # Ensure file exists on disk

        stop_event = threading.Event()
        msg_count = [len(s.messages)]
        save_count = [0]

        def periodic_checkpoint():
            while not stop_event.wait(0.1):
                try:
                    cur = len(s.messages)
                    if cur > msg_count[0]:
                        s.save(skip_index=True)
                        msg_count[0] = cur
                        save_count[0] += 1
                except Exception:
                    pass

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.5)  # Timer fires ~5 times, but no new messages
        stop_event.set()
        t.join(timeout=2)

        assert save_count[0] == 0, "Should not save when messages haven't changed"

    def test_checkpoint_timer_stops_on_signal(self):
        """The checkpoint thread exits cleanly when the stop event is set."""
        s = _make_session("ckpt3")
        stop_event = threading.Event()
        iterations = [0]

        def periodic_checkpoint():
            while not stop_event.wait(0.05):
                iterations[0] += 1

        t = threading.Thread(target=periodic_checkpoint, daemon=True)
        t.start()
        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=1)

        # Thread should have stopped; iterations should be bounded
        assert t.is_alive() is False

    def test_messages_survive_simulated_restart(self):
        """Messages saved by periodic checkpoint survive a 'process restart'.

        Simulates: agent adds messages → checkpoint saves → session object
        is discarded (simulating restart) → session reloaded from disk.
        """
        s = _make_session("survive1", messages=[{"role": "user", "content": "do stuff"}])
        s.save()  # Initial save

        # Simulate agent streaming: add messages
        s.messages.append({"role": "assistant", "content": "I'll help with that"})
        s.messages.append({"role": "assistant", "content": "Let me search for info"})
        s.messages.append({"role": "tool", "content": "search results here"})
        # Periodic checkpoint saves this state
        s.save(skip_index=True)

        # Simulate server restart: discard in-memory session, reload from disk
        del s
        models.SESSIONS.clear()

        reloaded = Session.load("survive1")
        assert reloaded is not None
        assert len(reloaded.messages) == 4  # user + assistant + assistant + tool
        assert reloaded.messages[1]["content"] == "I'll help with that"
        assert reloaded.messages[3]["content"] == "search results here"

    def test_skip_index_save_with_touch_updated_at_false(self):
        """save(skip_index=True, touch_updated_at=False) only writes JSON."""
        s = _make_session("touch1")
        original_updated_at = s.updated_at
        time.sleep(0.05)
        s.save(skip_index=True, touch_updated_at=False)
        data = json.loads(s.path.read_text())
        # updated_at should be the original value (not touched)
        assert data["updated_at"] == original_updated_at
        assert not models.SESSION_INDEX_FILE.exists()
