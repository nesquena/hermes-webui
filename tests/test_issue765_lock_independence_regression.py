"""#765 lock-independence regression, relocated out of the streaming-persistence file.

``tests/test_issue765_streaming_persistence.py`` is pinned byte-for-byte to the
audited baseline (see ``tests/test_audit_original765.py``) so a reviewer can
diff the streaming-persistence contract without noise. The D-series follow-up
coverage that used to live inside it is therefore kept in this dedicated file
instead of editing the pinned one.

This test pins the #765 write-serialization contract that the Resume ownership
fencing (F1-F4, Astra re-audit 94bfe8af) must not regress: unrelated sessions
sharing the process but not a session id must never queue behind one another's
disk write. The resume claim/quarantine machinery only ever touches the resume
publication path, so ordinary ``Session.save()`` stays fully lock-free.
"""
import threading

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


def test_distinct_sessions_do_not_serialize_on_one_write_lock(monkeypatch):
    """Unrelated sessions must never share one write lock (#765).

    The pre-fix module-global ``_SESSION_FILE_WRITE_LOCK`` serialized every
    save in the process, so one slow fsync stalled every conversation. Saves
    must stay fully lock-free: session B has to reach ``os.replace`` while
    session A is parked inside its own.
    """
    a = _make_session("lock_independent_a")
    b = _make_session("lock_independent_b")
    a.save(skip_index=True)
    b.save(skip_index=True)

    original_replace = models.os.replace
    first_replace_entered = threading.Event()
    release_first_replace = threading.Event()
    second_replace_entered = threading.Event()
    order = []
    errors = []

    def _replace_with_gate(src, dst):
        order.append(str(dst))
        if len(order) == 1:
            first_replace_entered.set()
            assert release_first_replace.wait(timeout=5)
        else:
            second_replace_entered.set()
        return original_replace(src, dst)

    monkeypatch.setattr(models.os, "replace", _replace_with_gate)

    def _save_worker(session):
        try:
            session.save(skip_index=True)
        except Exception as e:  # pragma: no cover - failure is asserted below
            errors.append(e)

    t1 = threading.Thread(target=_save_worker, args=(a,), daemon=True)
    t2 = threading.Thread(target=_save_worker, args=(b,), daemon=True)
    t1.start()
    assert first_replace_entered.wait(timeout=5)
    t2.start()
    # Session B must not wait behind session A's per-session lock.
    assert second_replace_entered.wait(timeout=5), (
        "unrelated session save was blocked by another session's write lock"
    )
    release_first_replace.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert not errors, f"Concurrent distinct-session saves should not fail: {errors}"