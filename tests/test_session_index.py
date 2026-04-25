"""
Tests for the incremental session index in api/models.py.

Validates:
  - Incremental patch correctness (existing entries preserved, updated)
  - New session appended to existing index
  - First call (no index file) triggers full rebuild
  - Corrupt index triggers fallback to full rebuild
  - Concurrent saves don't lose data
  - Atomic write leaves no .tmp file behind
  - Deadlock guard on fallback path
"""
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import api.models as models
from api.models import Session, _write_session_index


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp directory
    so tests don't touch the real session store.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    # Also patch the module-level references that Session uses
    monkeypatch.setattr(models.Session, "__module__", models.__name__)

    # Clear the in-memory SESSIONS cache to avoid bleed
    models.SESSIONS.clear()

    yield session_dir, index_file

    models.SESSIONS.clear()


def _make_session(session_id, title="Untitled", updated_at=None):
    """Helper to create a Session with a known ID and title."""
    s = Session(session_id=session_id, title=title, messages=[{"role": "user", "content": "hi"}])
    if updated_at is not None:
        s.updated_at = updated_at
    return s


def _write_index_file(index_file, entries):
    """Write entries list to the index file atomically."""
    tmp = index_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(index_file))


def _read_index(index_file):
    """Read and parse the session index file."""
    return json.loads(index_file.read_text(encoding="utf-8"))


# ── 6. test_incremental_patch_correctness ─────────────────────────────────

def test_incremental_patch_correctness():
    """Pre-write an index with 3 sessions (A, B, C). Create an updated
    Session for B with a new title. Call _write_session_index(updates=[B]).
    Verify A and C are unchanged, B has the new title, sort order preserved.
    """


    # We need to get the fixture values — but since it's autouse, the monkeypatch
    # has already been applied. Access the patched values directly.
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Create 3 sessions with different timestamps
    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)
    sC = _make_session("sess_c", "Charlie", updated_at=300.0)

    # Write session files to disk (so full rebuild can find them)
    for s in (sA, sB, sC):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build initial index
    _write_session_index(updates=None)
    index = _read_index(index_file)
    assert len(index) == 3

    # Now update B with a new title
    sB_updated = _make_session("sess_b", "Bravo Updated", updated_at=250.0)
    sB_updated.path.write_text(
        json.dumps(sB_updated.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Incremental update
    _write_session_index(updates=[sB_updated])

    # Verify
    index = _read_index(index_file)
    index_map = {e["session_id"]: e for e in index}

    assert index_map["sess_a"]["title"] == "Alpha", "A should be unchanged"
    assert index_map["sess_c"]["title"] == "Charlie", "C should be unchanged"
    assert index_map["sess_b"]["title"] == "Bravo Updated", "B should have new title"

    # Sort order: Charlie (300) > Bravo Updated (250) > Alpha (100)
    assert index[0]["session_id"] == "sess_c"
    assert index[1]["session_id"] == "sess_b"
    assert index[2]["session_id"] == "sess_a"


# ── 7. test_new_session_appended_to_index ─────────────────────────────────

def test_new_session_appended_to_index():
    """Pre-write index with sessions A, B. Call _write_session_index(updates=[C])
    where C is not in the existing index. Verify C appears in the index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)

    for s in (sA, sB):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=None)

    # Create a new session C not in the index
    sC = _make_session("sess_c", "Charlie", updated_at=300.0)
    sC.path.write_text(json.dumps(sC.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sC])

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_c" in ids, "New session C should appear in the index"
    assert "sess_a" in ids
    assert "sess_b" in ids


def test_incremental_update_prunes_stale_entries():
    """Ghost rows whose backing JSON file is gone must be dropped on the fast path.

    This covers session-id rotation paths (e.g. compression) where the old id can
    linger in `_index.json` after the file has been renamed.
    """
    index_file = models.SESSION_INDEX_FILE

    stale = {
        "session_id": "ghost_sid",
        "title": "Ghost",
        "updated_at": 150.0,
        "workspace": "/tmp",
        "model": "test",
        "message_count": 1,
        "created_at": 100.0,
        "pinned": False,
        "archived": False,
    }
    _write_index_file(index_file, [stale])

    sA = _make_session("sess_a", "Alpha", updated_at=200.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sA])

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids
    assert "ghost_sid" not in ids, "stale entry with no backing file must be pruned"


def test_load_metadata_only_does_not_parse_large_message_body():
    """Large sessions must keep the metadata-only path cheap."""
    s = Session(
        session_id="sess_large",
        title="Large Session",
        messages=[{"role": "assistant", "content": "x" * 200_000}],
        tool_calls=[{"id": "tool_1", "name": "read_file", "result": "y" * 10_000}],
        input_tokens=123,
        output_tokens=45,
    )
    s.save()

    with patch.object(Session, "load", side_effect=AssertionError("full load should not run")):
        meta = Session.load_metadata_only("sess_large")

    assert meta is not None
    assert meta.session_id == "sess_large"
    assert meta.title == "Large Session"
    assert meta.input_tokens == 123
    assert meta.output_tokens == 45
    assert meta.messages == []
    assert meta.tool_calls == []
    assert meta.compact()["message_count"] == 1


def test_metadata_only_get_session_does_not_poison_full_session_cache():
    s = Session(
        session_id="sess_cache",
        title="Cache Guard",
        messages=[{"role": "user", "content": "hi"}],
    )
    s.save(skip_index=True)

    meta = models.get_session("sess_cache", metadata_only=True)
    assert meta.messages == []
    assert "sess_cache" not in models.SESSIONS

    full = models.get_session("sess_cache")
    assert full.messages == [{"role": "user", "content": "hi"}]
    assert models.SESSIONS["sess_cache"] is full


def test_session_save_does_not_persist_metadata_message_count_hint():
    s = Session(
        session_id="sess_private_hint",
        title="Private Hint",
        messages=[{"role": "user", "content": "hi"}],
    )
    s._metadata_message_count = 10
    s.save(skip_index=True)

    payload = json.loads(s.path.read_text(encoding="utf-8"))
    assert "_metadata_message_count" not in payload


# ── 8. test_first_call_full_rebuild ──────────────────────────────────────

def test_first_call_full_rebuild():
    """When no index file exists, calling _write_session_index(updates=[session])
    should fall back to full rebuild and create the index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # No index file yet
    assert not index_file.exists()

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Call with updates — should trigger full rebuild since index doesn't exist
    _write_session_index(updates=[sA])

    # Index should now exist
    assert index_file.exists(), "Index file should be created"

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids, "Session A should appear in the rebuilt index"


# ── 9. test_corrupt_index_fallback ────────────────────────────────────────

def test_corrupt_index_fallback():
    """Write garbage/invalid JSON to SESSION_INDEX_FILE. Call
    _write_session_index(updates=[session]). Verify it falls back to
    full rebuild and the result is valid JSON with correct entries.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Write corrupt data
    index_file.write_text("THIS IS NOT JSON {{{", encoding="utf-8")

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Should not raise; should fall back to full rebuild
    _write_session_index(updates=[sA])

    # Index should now be valid JSON
    assert index_file.exists()
    index = _read_index(index_file)
    assert isinstance(index, list), "Index should be a list"

    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids, "Session A should appear after fallback rebuild"


# ── 10. test_concurrent_saves_dont_lose_data ────────────────────────────

def test_concurrent_saves_dont_lose_data():
    """Create 2 threads, each calling Session.save() on different sessions
    with a pre-existing index. Use a threading.Event barrier to force them
    to run concurrently. Assert both updates are present in the final index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)

    for s in (sA, sB):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build initial index
    _write_session_index(updates=None)

    # Now update both sessions concurrently
    barrier = threading.Event()
    errors = []

    def _update_session(session, new_title, new_updated_at):
        try:
            barrier.wait(timeout=5)
            session.title = new_title
            session.updated_at = new_updated_at
            session.save()
        except Exception as e:
            errors.append(e)

    sA.title = "Alpha V2"
    sA.updated_at = 150.0
    sB.title = "Bravo V2"
    sB.updated_at = 250.0

    t1 = threading.Thread(target=_update_session, args=(sA, "Alpha V2", 150.0))
    t2 = threading.Thread(target=_update_session, args=(sB, "Bravo V2", 250.0))

    t1.start()
    t2.start()

    # Release both threads simultaneously
    barrier.set()

    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Errors during concurrent saves: {errors}"

    # Verify both updates are in the final index
    index = _read_index(index_file)
    index_map = {e["session_id"]: e for e in index}

    assert "sess_a" in index_map, "Session A should be in index"
    assert "sess_b" in index_map, "Session B should be in index"
    assert index_map["sess_a"]["title"] == "Alpha V2", "Session A title should be updated"
    assert index_map["sess_b"]["title"] == "Bravo V2", "Session B title should be updated"


# ── 11. test_atomic_write_no_tmp_remains ─────────────────────────────────

def test_atomic_write_no_tmp_remains():
    """After _write_session_index completes, no .tmp file should remain
    in SESSION_DIR.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sA])

    # Check for any .tmp files in SESSION_DIR
    tmp_files = list(session_dir.glob("*.tmp"))
    assert len(tmp_files) == 0, f"Unexpected .tmp files remain: {tmp_files}"

    # Also test incremental path
    sA.title = "Alpha V2"
    sA.updated_at = 200.0
    _write_session_index(updates=[sA])

    tmp_files = list(session_dir.glob("*.tmp"))
    assert len(tmp_files) == 0, f"Unexpected .tmp files after incremental write: {tmp_files}"


# ── 12. test_deadlock_guard_on_fallback ──────────────────────────────────

def test_deadlock_guard_on_fallback():
    """Mock the index file read to raise an exception, then verify
    _write_session_index(updates=[session]) completes without hanging.

    This tests that the fallback path (corrupt index -> full rebuild)
    is called outside the LOCK, so it doesn't deadlock.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Create a valid index file so the incremental path is attempted
    _write_index_file(index_file, [
        {"session_id": "sess_a", "title": "Alpha", "updated_at": 100.0,
         "workspace": "/tmp", "model": "test", "message_count": 0,
         "created_at": 100.0, "pinned": False, "archived": False},
    ])

    sB = _make_session("sess_b", "Bravo", updated_at=200.0)
    sB.path.write_text(json.dumps(sB.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Make the index file read raise an exception to trigger fallback
    original_read_text = Path.read_text
    call_count = 0

    def _broken_read_text(self, *args, **kwargs):
        nonlocal call_count
        # Only break the index file read, not the session file reads
        if str(self) == str(index_file) and call_count == 0:
            call_count += 1
            raise OSError("Simulated corrupt index read")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _broken_read_text):
        # This should complete without hanging (deadlock guard)
        # Use a timeout to detect deadlock
        done = threading.Event()
        result = [None]
        exc = [None]

        def _run():
            try:
                _write_session_index(updates=[sB])
                result[0] = "done"
            except Exception as e:
                exc[0] = e
            finally:
                done.set()

        t = threading.Thread(target=_run)
        t.start()
        finished = done.wait(timeout=10)

        assert finished, "_write_session_index hung — likely deadlock in fallback path"
        assert exc[0] is None, f"Unexpected exception: {exc[0]}"

    # The index should still be valid after fallback
    index = _read_index(index_file)
    assert isinstance(index, list)


def test_incremental_index_disk_io_runs_outside_lock(monkeypatch):
    """Fast-path disk I/O (fsync/replace) must run after releasing LOCK."""
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_session_index(updates=None)  # seed index

    sA.title = "Alpha V2"
    sA.updated_at = 200.0

    fsync_lock_states = []
    original_fsync = models.os.fsync

    def _observing_fsync(fd):
        fsync_lock_states.append(models.LOCK.locked())
        return original_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _observing_fsync)

    _write_session_index(updates=[sA])

    assert fsync_lock_states, "Expected at least one fsync call during index write"
    assert not any(fsync_lock_states), (
        "_write_session_index fast path must not hold LOCK during fsync/disk I/O"
    )


def test_full_rebuild_index_disk_io_runs_outside_lock(monkeypatch):
    """Full-rebuild disk I/O (fsync/replace) must run after releasing LOCK."""
    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    fsync_lock_states = []
    original_fsync = models.os.fsync

    def _observing_fsync(fd):
        fsync_lock_states.append(models.LOCK.locked())
        return original_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _observing_fsync)

    _write_session_index(updates=None)

    assert fsync_lock_states, "Expected at least one fsync call during index write"
    assert not any(fsync_lock_states), (
        "_write_session_index full rebuild must not hold LOCK during fsync/disk I/O"
    )


def test_all_sessions_ignores_stale_index_entries():
    """Reading via all_sessions() must not surface ghost rows from _index.json."""
    index_file = models.SESSION_INDEX_FILE

    valid_session = _make_session("sess_a", "Alpha", updated_at=200.0)
    valid_session.path.write_text(
        json.dumps(valid_session.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    valid = valid_session.compact()
    stale = {
        "session_id": "ghost_sid",
        "title": "Ghost",
        "updated_at": 150.0,
        "workspace": "/tmp",
        "model": "test",
        "message_count": 1,
        "created_at": 100.0,
        "pinned": False,
        "archived": False,
    }
    _write_index_file(index_file, [stale, valid])

    rows = models.all_sessions()
    ids = {e["session_id"] for e in rows}
    assert "sess_a" in ids
    assert "ghost_sid" not in ids
