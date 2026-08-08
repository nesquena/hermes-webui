"""Regression tests for #3802 — deleting a session must remove its journal files.

Deleting a conversation from the WebUI removed the session JSON + state.db rows
but left the turn journal (`_turn_journal/{sid}*.jsonl`, user messages in
plaintext) and the run journal (`_run_journal/{sid}/`, full request/response
payloads) on disk, so the conversation stayed recoverable. These tests pin the
two cleanup helpers: every shard/dir for the deleted session is removed, and an
unrelated session's journals are left untouched.
"""
import os
import shutil
from pathlib import Path

import pytest

import api.run_journal as run_journal
from api.run_journal import RunJournalWriter, delete_run_journal, read_run_events
from api.turn_journal import (
    append_turn_journal_event,
    delete_turn_journal,
    read_turn_journal,
)


def _submit(sid, content, session_dir):
    return append_turn_journal_event(
        sid,
        {"event": "submitted", "turn_id": "t1", "stream_id": "s1", "role": "user", "content": content},
        session_dir=session_dir,
    )


def test_delete_turn_journal_removes_pid_shard_and_legacy(tmp_path):
    # pid-scoped shard (written by append) + a legacy single-file shard.
    _submit("sid-del", "secret message", session_dir=tmp_path)
    journal_dir = tmp_path / "_turn_journal"
    legacy = journal_dir / "sid-del.jsonl"
    legacy.write_text('{"event":"submitted","session_id":"sid-del","content":"old"}\n', encoding="utf-8")

    pid_shard = journal_dir / f"sid-del~{os.getpid()}.jsonl"
    assert pid_shard.exists()
    assert legacy.exists()

    removed = delete_turn_journal("sid-del", session_dir=tmp_path)

    assert removed == 2
    assert not pid_shard.exists()
    assert not legacy.exists()
    # read_turn_journal now finds nothing for the deleted session.
    assert read_turn_journal("sid-del", session_dir=tmp_path)["events"] == []


def test_delete_turn_journal_leaves_other_sessions_intact(tmp_path):
    _submit("sid-keep", "keep me", session_dir=tmp_path)
    _submit("sid-del", "delete me", session_dir=tmp_path)

    delete_turn_journal("sid-del", session_dir=tmp_path)

    keep_shard = tmp_path / "_turn_journal" / f"sid-keep~{os.getpid()}.jsonl"
    del_shard = tmp_path / "_turn_journal" / f"sid-del~{os.getpid()}.jsonl"
    assert keep_shard.exists(), "unrelated session's journal must survive"
    assert not del_shard.exists()
    assert read_turn_journal("sid-keep", session_dir=tmp_path)["events"]


def test_delete_turn_journal_noop_on_missing_or_invalid(tmp_path):
    # Missing directory: no error, zero removed.
    assert delete_turn_journal("nope", session_dir=tmp_path) == 0
    # Invalid id: no error, zero removed (and never touches the filesystem).
    assert delete_turn_journal("../etc/passwd", session_dir=tmp_path) == 0
    assert delete_turn_journal("", session_dir=tmp_path) == 0


def test_delete_run_journal_removes_session_directory(tmp_path):
    writer = RunJournalWriter("sid-del", "run-1", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hello"})
    writer.append_sse_event("done", {"session": {"session_id": "sid-del"}})
    run_dir = tmp_path / "_run_journal" / "sid-del"
    assert run_dir.exists()

    assert delete_run_journal("sid-del", session_dir=tmp_path) is True
    assert not run_dir.exists()
    # No events recoverable after delete.
    assert read_run_events("sid-del", "run-1", session_dir=tmp_path)["events"] == []


def test_delete_run_journal_leaves_other_sessions_intact(tmp_path):
    RunJournalWriter("sid-keep", "run-k", session_dir=tmp_path).append_sse_event("token", {"text": "k"})
    RunJournalWriter("sid-del", "run-d", session_dir=tmp_path).append_sse_event("token", {"text": "d"})

    delete_run_journal("sid-del", session_dir=tmp_path)

    assert (tmp_path / "_run_journal" / "sid-keep").exists()
    assert not (tmp_path / "_run_journal" / "sid-del").exists()


def test_delete_run_journal_noop_on_missing_or_invalid(tmp_path):
    assert delete_run_journal("nope", session_dir=tmp_path) is False
    assert delete_run_journal("../escape", session_dir=tmp_path) is False
    assert delete_run_journal("", session_dir=tmp_path) is False


def test_delete_run_journal_surfaces_rmtree_failure_without_evicting_caches(
    tmp_path, monkeypatch
):
    writer = RunJournalWriter("sid-delete-failure", "run-1", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "keep"})
    run_path = tmp_path / "_run_journal" / "sid-delete-failure" / "run-1.jsonl"
    dir_key = str(run_path.parent)
    run_key = str(run_path)
    run_journal.latest_run_summary(
        "sid-delete-failure", "run-1", session_dir=tmp_path
    )
    with run_journal._WRITER_LOCKS_GUARD:
        writer_locks_before = {
            key: lock
            for key, lock in run_journal._WRITER_LOCKS.items()
            if key[0] == dir_key
        }
    with run_journal._SEQ_CACHE_LOCK:
        seq_cache_before = {
            key: value
            for key, value in run_journal._SEQ_CACHE.items()
            if str(Path(key).parent) == dir_key
        }
    with run_journal._SUMMARY_CACHE_LOCK:
        summary_cache_before = {
            key: value
            for key, value in run_journal._SUMMARY_CACHE.items()
            if str(Path(key).parent) == dir_key
        }
    assert writer_locks_before
    assert run_key in seq_cache_before
    assert run_key in summary_cache_before

    def fail_rmtree(path, *, ignore_errors=False):
        raise OSError("forced-rmtree-failure")

    monkeypatch.setattr(shutil, "rmtree", fail_rmtree)
    with pytest.raises(OSError, match="forced-rmtree-failure"):
        delete_run_journal("sid-delete-failure", session_dir=tmp_path)

    assert (tmp_path / "_run_journal" / "sid-delete-failure").exists()
    with run_journal._WRITER_LOCKS_GUARD:
        writer_locks_after = {
            key: lock
            for key, lock in run_journal._WRITER_LOCKS.items()
            if key[0] == dir_key
        }
    with run_journal._SEQ_CACHE_LOCK:
        seq_cache_after = {
            key: value
            for key, value in run_journal._SEQ_CACHE.items()
            if str(Path(key).parent) == dir_key
        }
    with run_journal._SUMMARY_CACHE_LOCK:
        summary_cache_after = {
            key: value
            for key, value in run_journal._SUMMARY_CACHE.items()
            if str(Path(key).parent) == dir_key
        }
    assert writer_locks_after.keys() == writer_locks_before.keys()
    for key, lock in writer_locks_before.items():
        assert writer_locks_after[key] is lock
    assert seq_cache_after == seq_cache_before
    assert summary_cache_after == summary_cache_before


def test_delete_journals_reject_dot_traversal_ids(tmp_path):
    """A bare '.'/'..' passes the dot-permitting id regex but must NOT resolve to
    the journal root/parent and delete the wrong directory (no '/' to catch it).
    """
    # Seed a real run + turn journal so we'd notice an over-broad delete.
    writer = RunJournalWriter("keep", "run-1", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "hello"})
    _submit("keep", "hi", tmp_path)
    run_dir = tmp_path / "_run_journal" / "keep"
    assert run_dir.exists()
    for bad in (".", ".."):
        assert delete_run_journal(bad, session_dir=tmp_path) is False
        assert delete_turn_journal(bad, session_dir=tmp_path) == 0
    # The legitimate journals must still be present.
    assert run_dir.exists()
    assert read_turn_journal("keep", session_dir=tmp_path)
