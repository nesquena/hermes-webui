"""Regression tests for #4633/#2097 — deleting a run journal must evict its writer lock.

`_lock_for` lazily creates a `threading.Lock` per ``(dir, file, pid)`` key and
caches it in the module-global `_WRITER_LOCKS`, but nothing ever removed those
entries: `delete_run_journal` rmtree'd the on-disk directory yet left the lock
objects behind, so a long-lived gateway leaked one entry per deleted run
forever. These tests pin that delete now drops the matching cache entries while
leaving unrelated sessions' locks intact.
"""
import api.run_journal as run_journal
from api.run_journal import RunJournalWriter, delete_run_journal


def _keys_for(session_dir, sid):
    dir_key = str(session_dir / run_journal.RUN_JOURNAL_DIR_NAME / sid)
    return [k for k in run_journal._WRITER_LOCKS if k[0] == dir_key]


def _writer(sid, run_id, session_dir):
    incarnation = run_journal.activate_run_journal_session(
        sid,
        session_dir=session_dir,
    )
    return RunJournalWriter(
        sid,
        run_id,
        session_dir=session_dir,
        incarnation=incarnation,
    )


def test_delete_run_journal_evicts_writer_locks(tmp_path):
    writer = _writer("sid-del", "run-1", tmp_path)
    writer.append_sse_event("token", {"text": "hello"})
    # Constructing the writer + appending populated the per-run lock cache.
    assert _keys_for(tmp_path, "sid-del")

    assert delete_run_journal("sid-del", session_dir=tmp_path) is True

    # The cached lock(s) for the deleted session are gone — no unbounded climb.
    assert _keys_for(tmp_path, "sid-del") == []


def test_delete_run_journal_keeps_other_sessions_locks(tmp_path):
    _writer("sid-keep", "run-k", tmp_path).append_sse_event("token", {"text": "k"})
    _writer("sid-del", "run-d", tmp_path).append_sse_event("token", {"text": "d"})

    delete_run_journal("sid-del", session_dir=tmp_path)

    assert _keys_for(tmp_path, "sid-del") == []
    assert _keys_for(tmp_path, "sid-keep"), "unrelated session's lock must survive"


def test_delete_run_journal_noop_leaves_cache_untouched(tmp_path):
    _writer("sid-keep", "run-k", tmp_path).append_sse_event("token", {"text": "k"})
    before = _keys_for(tmp_path, "sid-keep")

    # Missing/invalid ids never touch the cache.
    assert delete_run_journal("nope", session_dir=tmp_path) is False
    assert delete_run_journal("../escape", session_dir=tmp_path) is False

    assert _keys_for(tmp_path, "sid-keep") == before
