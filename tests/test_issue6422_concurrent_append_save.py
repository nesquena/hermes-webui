"""Tests for issue #6422: cross-client concurrent-append save."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
from collections import OrderedDict
from pathlib import Path

import pytest

import api.models as models
from api.models import (
    Session,
    _fsync_parent_dir,
    _merge_concurrent_message_arrays,
    _message_rows_match,
    _session_sidecar_cross_process_lock,
)
from api.session_recovery import (
    recover_missing_sidecars_from_state_db,
    recover_session,
)


@pytest.fixture
def temp_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr("api.config.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.config.SESSIONS", OrderedDict())
    return session_dir


def test_merge_concurrent_message_arrays_prefix_and_ordering():
    """Longest matching prefix is kept, disk rows beyond it are inserted before our tail."""
    base = [
        {"role": "system", "content": "sys", "id": 1},
        {"role": "user", "content": "hi", "id": 2},
    ]
    disk_tail = [
        {"role": "assistant", "content": "disk reply 1", "id": 3},
        {"role": "user", "content": "disk reply 2", "id": 4},
    ]
    our_tail = [
        {"role": "assistant", "content": "our reply 1", "id": 5},
    ]

    disk_msgs = base + disk_tail
    our_msgs = base + our_tail

    merged = _merge_concurrent_message_arrays(disk_msgs, our_msgs, allow_zero_prefix_merge=False)
    assert merged == base + disk_tail + our_tail


def test_merge_concurrent_message_arrays_identity_rules():
    """Test identity rules per position: stable id, id+role+content, and role+content."""
    # 1. Stable row identity when both carry one: equal ids with same content match
    row_a = {"role": "user", "content": "msg", "id": 1}
    row_b = {"role": "user", "content": "msg", "id": 1}
    assert _message_rows_match(row_a, row_b) is True

    # Equal ids with different role/content must diverge
    row_diff_content = {"role": "user", "content": "different", "id": 1}
    assert _message_rows_match(row_a, row_diff_content) is False

    row_diff_role = {"role": "assistant", "content": "msg", "id": 1}
    assert _message_rows_match(row_a, row_diff_role) is False

    # Different ids diverge
    row_diff_id = {"role": "user", "content": "msg", "id": 2}
    assert _message_rows_match(row_a, row_diff_id) is False

    # No ids: role + content equality
    no_id_1 = {"role": "user", "content": "text"}
    no_id_2 = {"role": "user", "content": "text"}
    no_id_3 = {"role": "user", "content": "other"}
    assert _message_rows_match(no_id_1, no_id_2) is True
    assert _message_rows_match(no_id_1, no_id_3) is False


def test_merge_concurrent_message_arrays_zero_prefix_handling():
    """Zero-length prefix merges only when allow_zero_prefix_merge is True."""
    disk = [{"role": "user", "content": "a", "id": 1}]
    our = [{"role": "user", "content": "b", "id": 2}]

    # Default False: our rows survive, no merge
    assert _merge_concurrent_message_arrays(disk, our, allow_zero_prefix_merge=False) == our

    # True: disk rows prepended before our tail
    assert _merge_concurrent_message_arrays(disk, our, allow_zero_prefix_merge=True) == disk + our


def test_concurrent_append_two_writers_both_saved(temp_session_dir):
    """Two writers appending with append_intent=True retain both turns in order."""
    sid = "sess_conc_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "initial question", "id": 1}]
    base.save()

    # Client A loads from disk and prepares its turn
    client_a = Session.load(sid)
    client_a.messages.append({"role": "assistant", "content": "answer from client A", "id": 2})

    # Client B loads from disk (at base) and prepares its turn
    client_b = Session.load(sid)
    client_b.messages.append({"role": "user", "content": "question from client B", "id": 3})

    # Client A saves first
    client_a.save(append_intent=True)

    # Client B saves second with append_intent=True
    client_b.save(append_intent=True)

    # Reload fresh session
    loaded = Session.load(sid)
    assert len(loaded.messages) == 3
    assert loaded.messages[0]["content"] == "initial question"
    assert loaded.messages[1]["content"] == "answer from client A"
    assert loaded.messages[2]["content"] == "question from client B"
    assert loaded._metadata_message_count == 3


def test_control_two_writers_default_intent_last_writer_wins(temp_session_dir):
    """Control test: two writers with default append_intent=False keep last-writer-wins."""
    sid = "sess_control_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "initial question", "id": 1}]
    base.save()

    client_a = Session.load(sid)
    client_a.messages.append({"role": "assistant", "content": "answer from client A", "id": 2})

    client_b = Session.load(sid)
    client_b.messages.append({"role": "user", "content": "question from client B", "id": 3})

    # Both save without append_intent (default False)
    client_a.save()
    client_b.save()

    loaded = Session.load(sid)
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["content"] == "initial question"
    assert loaded.messages[1]["content"] == "question from client B"
    # Client A's turn was overwritten by client B


def test_one_empty_context_messages_reconciliation(temp_session_dir):
    """One-empty context reconciliation adopts or keeps according to emptiness."""
    sid = "sess_ctx_reconcile"

    # Case 1: Disk has context_messages tail, our copy has empty context_messages
    s = Session(session_id=sid)
    s.messages = [{"role": "user", "content": "hello", "id": 1}]
    s.context_messages = [
        {"role": "user", "content": "hello", "id": 1},
        {"role": "assistant", "content": "disk ctx", "id": 2},
    ]
    s.save()

    writer = Session(session_id=sid)
    writer.messages = [
        {"role": "user", "content": "hello", "id": 1},
        {"role": "user", "content": "our turn", "id": 3},
    ]
    writer.context_messages = []  # Empty!
    writer.save(append_intent=True)

    loaded = Session.load(sid)
    assert len(loaded.context_messages) == 2
    assert loaded.context_messages[1]["content"] == "disk ctx"

    # Case 2: Disk context is empty, our copy is non-empty -> keeps ours
    sid2 = "sess_ctx_reconcile_2"
    s2 = Session(session_id=sid2)
    s2.messages = [{"role": "user", "content": "hello", "id": 1}]
    s2.context_messages = []
    s2.save()

    writer2 = Session(session_id=sid2)
    writer2.messages = [
        {"role": "user", "content": "hello", "id": 1},
        {"role": "user", "content": "our turn", "id": 2},
    ]
    writer2.context_messages = [
        {"role": "user", "content": "hello", "id": 1},
        {"role": "user", "content": "our turn", "id": 2},
    ]
    writer2.save(append_intent=True)

    loaded2 = Session.load(sid2)
    assert len(loaded2.context_messages) == 2
    assert loaded2.context_messages[1]["content"] == "our turn"


def test_gateway_success_and_cancellation_revert_behavior(temp_session_dir):
    """Gateway success save keeps concurrent append; cancellation revert republishes base."""
    sid = "sess_gw_1"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "prompt", "id": 1}]
    base.save()

    # Concurrent writer appends on disk
    client2 = Session.load(sid)
    client2.messages.append({"role": "user", "content": "concurrent client msg", "id": 2})
    client2.save(append_intent=True)

    # Gateway success writeback saves with append_intent=True
    gw_session = Session(session_id=sid)
    gw_session.messages = [
        {"role": "user", "content": "prompt", "id": 1},
        {"role": "assistant", "content": "gw completion", "id": 3},
    ]
    gw_session.save(append_intent=True)

    loaded = Session.load(sid)
    assert len(loaded.messages) == 3
    assert loaded.messages[1]["content"] == "concurrent client msg"
    assert loaded.messages[2]["content"] == "gw completion"

    # Now test cancellation revert: does NOT opt into append_intent
    sid_cancel = "sess_gw_cancel"
    initial_messages = [{"role": "user", "content": "turn 1", "id": 1}]
    base_cancel = Session(session_id=sid_cancel)
    base_cancel.messages = list(initial_messages)
    base_cancel.save()

    # Another writer appends a turn on disk
    other = Session.load(sid_cancel)
    other.messages.append({"role": "assistant", "content": "other answer", "id": 2})
    other.save(append_intent=True)

    # Cancelled gateway execution reverts transcript to initial_messages without append_intent
    revert_session = Session(session_id=sid_cancel)
    revert_session.messages = list(initial_messages)
    # Revert save has append_intent=False (default)
    revert_session.save()

    reverted = Session.load(sid_cancel)
    # The cancelled revert republishes initial_messages, without merging
    assert len(reverted.messages) == 1
    assert reverted.messages[0]["content"] == "turn 1"


def test_durability_eio_propagates_and_leaves_baselines_intact(temp_session_dir, monkeypatch):
    """Directory fsync raising EIO propagates out of save() and preserves commit baselines."""
    sid = "sess_dur_eio"
    s = Session(session_id=sid)
    s.messages = [{"role": "user", "content": "hello", "id": 1}]
    s.reasoning_effort = "high"
    s.share_token = "token123"
    s.share_created_at = 12345.0
    s._reasoning_effort_saved = "low"
    s._share_token_saved = "token_old"
    s._share_created_at_saved = 10000.0

    def fail_fsync(path):
        raise OSError(errno.EIO, "I/O error on directory fsync")

    monkeypatch.setattr(models, "_fsync_parent_dir", fail_fsync)

    with pytest.raises(OSError) as exc_info:
        s.save()

    assert exc_info.value.errno == errno.EIO
    # In-memory baselines must remain unchanged
    assert s._reasoning_effort_saved == "low"
    assert s._share_token_saved == "token_old"
    assert s._share_created_at_saved == 10000.0


def test_durability_unsupported_errors_degrade_to_success(temp_session_dir, monkeypatch):
    """Unsupported directory fsync errors (EINVAL, EBADF, PermissionError) degrade to success."""
    sid = "sess_dur_unsupported"
    s = Session(session_id=sid)
    s.messages = [{"role": "user", "content": "hello", "id": 1}]
    s.reasoning_effort = "medium"
    s.share_token = "tok"
    s.share_created_at = 20000.0
    s._reasoning_effort_saved = "low"

    real_open = os.open
    real_fsync = os.fsync
    dir_fds = set()

    def tracked_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        try:
            if Path(path).is_dir():
                dir_fds.add(fd)
        except Exception:
            pass
        return fd

    monkeypatch.setattr("os.open", tracked_open)

    # Test that _fsync_parent_dir absorbs unsupported errors from the underlying OS
    for err in (errno.EINVAL, errno.EBADF, errno.EACCES):
        def fake_fsync(fd, e=err):
            if fd in dir_fds:
                raise OSError(e, "unsupported directory fsync")
            return real_fsync(fd)

        monkeypatch.setattr("os.fsync", fake_fsync)
        _fsync_parent_dir(s.path)
        s.save()
        assert s._reasoning_effort_saved == "medium"

    # Also test PermissionError directly on directory open
    def perm_open(path, flags, *args, **kwargs):
        try:
            if Path(path).is_dir():
                raise PermissionError(13, "Permission denied")
        except PermissionError:
            raise
        except Exception:
            pass
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("os.open", perm_open)
    _fsync_parent_dir(s.path)
    s.save()
    assert s._reasoning_effort_saved == "medium"


def test_fsync_parent_dir_handles_real_paths(tmp_path):
    """_fsync_parent_dir handles files and directories without crashing on Windows."""
    test_file = tmp_path / "sub" / "file.txt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("content", encoding="utf-8")

    # Should succeed or degrade safely on Windows/POSIX
    _fsync_parent_dir(test_file)
    _fsync_parent_dir(test_file.parent)


def test_recovery_session_from_backup_restores(temp_session_dir):
    """recover_session restores a session when .bak has more messages than the live sidecar."""
    sid = "sess_recovery_1"
    session_file = temp_session_dir / f"{sid}.json"
    bak_file = temp_session_dir / f"{sid}.json.bak"

    live_payload = {
        "session_id": sid,
        "title": "live",
        "messages": [{"role": "user", "content": "live msg", "id": 1}],
        "message_count": 1,
    }
    bak_payload = {
        "session_id": sid,
        "title": "bak",
        "messages": [
            {"role": "user", "content": "live msg", "id": 1},
            {"role": "assistant", "content": "bak reply", "id": 2},
        ],
        "message_count": 2,
    }
    session_file.write_text(json.dumps(live_payload), encoding="utf-8")
    bak_file.write_text(json.dumps(bak_payload), encoding="utf-8")

    result = recover_session(session_file)
    assert result.get("restored") is True

    loaded = Session.load(sid)
    assert len(loaded.messages) == 2
    assert loaded.messages[1]["content"] == "bak reply"


def test_recover_missing_sidecars_from_state_db(temp_session_dir, tmp_path):
    """recover_missing_sidecars_from_state_db materializes missing sidecar from state.db."""
    state_db_path = tmp_path / "state.db"
    conn = sqlite3.connect(state_db_path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    sid = "sess_state_db_rec"
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Test DB Session", "openai/gpt-4", 1000.0, 1, None),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (sid, "user", "from state db", 1000.0),
    )
    conn.commit()
    conn.close()

    result = recover_missing_sidecars_from_state_db(temp_session_dir, state_db_path)
    assert result["materialized"] == 1

    loaded = Session.load(sid)
    assert loaded.title == "Test DB Session"
    assert len(loaded.messages) == 1
    assert loaded.messages[0]["content"] == "from state db"


def test_locking_unavailable_degrades_without_merge(temp_session_dir, monkeypatch):
    """When locking is unavailable, append_intent degrades to today's behavior (no merge, no raise)."""
    sid = "sess_no_locks"
    base = Session(session_id=sid)
    base.messages = [{"role": "user", "content": "base", "id": 1}]
    base.save()

    # Writer A
    w1 = Session.load(sid)
    w1.messages.append({"role": "assistant", "content": "a1", "id": 2})

    # Writer B
    w2 = Session.load(sid)
    w2.messages.append({"role": "user", "content": "b1", "id": 3})

    w1.save()

    # Monkeypatch both fcntl and msvcrt to None
    monkeypatch.setattr(models, "_fcntl", None)
    monkeypatch.setattr(models, "_msvcrt", None)

    # Calling cross process lock directly raises
    with pytest.raises(RuntimeError, match="locking is unavailable"):
        with _session_sidecar_cross_process_lock(sid):
            pass

    # Save with append_intent=True degrades cleanly (no merge, no raise)
    w2.save(append_intent=True)

    loaded = Session.load(sid)
    # Without locking available, it degraded to today's behavior (overwrote disk with w2)
    assert len(loaded.messages) == 2
    assert loaded.messages[1]["content"] == "b1"


def test_corrupted_disk_sidecar_degrades_gracefully(temp_session_dir):
    """When disk sidecar is corrupt or not a dict, append_intent degrades cleanly."""
    sid = "sess_corrupt"
    sidecar_path = temp_session_dir / f"{sid}.json"
    sidecar_path.write_text("not json at all!!!", encoding="utf-8")

    s = Session(session_id=sid)
    s.messages = [{"role": "user", "content": "clean", "id": 1}]
    # Should not raise
    s.save(append_intent=True)

    loaded = Session.load(sid)
    assert len(loaded.messages) == 1
    assert loaded.messages[0]["content"] == "clean"
