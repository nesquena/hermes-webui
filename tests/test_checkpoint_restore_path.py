"""Tests for the WebUI checkpoint-restore path (POST /api/session/checkpoint/restore).

Covers the issues raised in PR #7075 review #2:
  * durable row_id contract (integer only, fail closed, no legacy m.id)
  * read_only ownership gate
  * pending/live-turn pre-checks + the Agent's in-transaction guards
  * the durable archive commits BEFORE the sidecar write, through
    SessionDB.rewind_to_message (no hand-rolled SQLite UPDATE)
  * a sidecar publish failure after the durable commit is fail-closed
    (flag + crash-safe resync marker re-applied on the next load)
  * the weaker legacy sink /api/session/truncate-before is gone

Production-composed: the tmp state.db is built with the REAL agent schema and
its rows are written through the SAME SessionDB API the agent uses, so the
authority path (schema init, in-txn CAS, lease guards, counters) is exercised
for real — not against a stub table.
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api.config as config  # noqa: E402
import api.models as models  # noqa: E402
import api.routes as routes  # noqa: E402
import api.session_ops as session_ops  # noqa: E402
from api.models import Session  # noqa: E402


class _FakeHandler:
    """Minimal stand-in for the HTTP handler used by _read_json_body."""

    def __init__(self, body: dict):
        raw = json.dumps(body).encode("utf-8")
        raw_len = len(raw)
        self.rfile = type("RFile", (), {"read": staticmethod(lambda n=raw_len: raw)})()
        self.close_connection = False
        self._status = None
        self._json = None

    def send_response(self, code):
        self._status = code

    def _send_json(self, payload, status=200):
        self._status = status
        self._json = payload

    def _send_json_error(self, status, message):
        self._status = status
        self._json = {"ok": False, "error": message}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _seed_state_db(state_db, sid: str, rows):
    """Build the tmp state.db through the real SessionDB API; returns row ids."""
    from hermes_state import SessionDB
    db = SessionDB(state_db)
    try:
        db.ensure_session(sid, source="webui-test")
        return [db.append_message(sid, role, content) for role, content in rows]
    finally:
        db.close()


def _active_ids(state_db, sid):
    conn = sqlite3.connect(state_db)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE session_id=? "
            "AND (active IS NULL OR active != 0)", (sid,)).fetchall())
    finally:
        conn.close()


def _rewind_count(state_db, sid):
    conn = sqlite3.connect(state_db)
    try:
        row = conn.execute(
            "SELECT COALESCE(rewind_count, 0) FROM sessions WHERE id=?", (sid,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A sidecar session with stamped _row_id + a REAL mirrored state.db.

    Layout (durable ids come back from append_message):
      idx 0: user  u1
      idx 1: asst  a1
      idx 2: user  u2   <- checkpoint target
      idx 3: asst  a2
    """
    sid = "restoretest01"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_db = tmp_path / "state.db"

    u1, a1, u2, a2 = _seed_state_db(state_db, sid, [
        ("user", "first prompt"), ("assistant", "answer 1"),
        ("user", "second prompt"), ("assistant", "answer 2"),
    ])
    ids = {"u1": u1, "a1": a1, "u2": u2, "a2": a2}

    s = Session(session_id=sid)
    s.title = "restore test"
    s.messages = [
        {"role": "user", "content": "first prompt", "id": "u1", "_row_id": u1},
        {"role": "assistant", "content": "answer 1", "id": "a1", "_row_id": a1},
        {"role": "user", "content": "second prompt", "id": "u2", "_row_id": u2},
        {"role": "assistant", "content": "answer 2", "id": "a2", "_row_id": a2},
    ]
    s.context_messages = [dict(m) for m in s.messages]

    monkeypatch.setattr(config, "SESSION_DIR", str(sessions_dir))
    # Session.path is a property over models.SESSION_DIR (a Path) — redirect it
    # so save()/load() hit the tmp store, not the real one.
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    # state.db resolution: the WebUI reader path (_active_state_db_path) and the
    # profile-aware authority path (_agent_state_db_path) must both land on the
    # tmp database — session_ops/checkpoint_map resolve them inside each call.
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda **kw: state_db)
    s.save()
    # The route layer looks sessions up via get_session / SESSIONS.
    monkeypatch.setitem(models.SESSIONS, sid, s)
    return SimpleNamespace(sid=sid, session=s, state_db=state_db, ids=ids)


# --------------------------------------------------------------------------
# backend contract tests (direct — the route delegates 1:1 to this function)
# --------------------------------------------------------------------------

def test_restore_requires_int_row_id_fails_closed(env):
    sid = env.sid
    # non-integer rejected
    for bad in ("7", None, 0, -3, 1.5):
        with pytest.raises((ValueError, TypeError)):
            session_ops.restore_checkpoint_at_row_id(sid, bad)


def test_restore_unknown_row_id_fails_closed(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 999999)


def test_restore_to_user_checkpoint_truncates_and_archives(env):
    res = session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert res["restored_to_row_id"] == env.ids["u2"]
    assert res["new_message_count"] == 2  # prefix u1/a1 survives
    assert res["archived_state_row_ids"] == [env.ids["u2"], env.ids["a2"]]
    # sidecar now truncated...
    msgs = env.session.messages
    assert [m["content"] for m in msgs] == ["first prompt", "answer 1"]
    # ...AND state.db suffix archived (same durable ids)
    assert _active_ids(env.state_db, env.sid) == [env.ids["u1"], env.ids["a1"]]


def test_restore_commits_through_agent_transaction(env):
    """The durable commit is a real rewind: rewind_count + counters updated
    by the Agent's own transaction (the review's item #2)."""
    before = _rewind_count(env.state_db, env.sid)
    session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert _rewind_count(env.state_db, env.sid) == before + 1
    conn = sqlite3.connect(env.state_db)
    try:
        count = conn.execute(
            "SELECT message_count FROM sessions WHERE id=?", (env.sid,)).fetchone()[0]
    finally:
        conn.close()
    assert int(count) == 2  # survivors after the soft-archive


def test_restore_cas_rejects_stale_snapshot(env, monkeypatch):
    """In-txn active-set CAS: if the active set changed under the snapshot the
    rewind refuses (RuntimeError in-txn) and nothing is written."""
    from hermes_state import SessionDB
    monkeypatch.setattr(SessionDB, "get_active_message_ids", lambda self, sid: [])
    with pytest.raises(session_ops.CheckpointStaleError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert len(env.session.messages) == 4
    assert _active_ids(env.state_db, env.sid) == [
        env.ids["u1"], env.ids["a1"], env.ids["u2"], env.ids["a2"]]


def test_restore_refuses_live_turn_lease_via_authority(env):
    """A REAL cross-process turn lease in state.db refuses the write inside the
    transaction (SessionTurnLeaseLostError -> CheckpointBusyError -> 409)."""
    from hermes_state import SessionDB
    db = SessionDB(env.state_db)
    try:
        assert db.try_acquire_session_turn_lease(
            env.sid, "webui-test-holder", ttl_seconds=120.0)
    finally:
        db.close()
    with pytest.raises(session_ops.CheckpointBusyError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert len(env.session.messages) == 4
    assert _active_ids(env.state_db, env.sid) == [
        env.ids["u1"], env.ids["a1"], env.ids["u2"], env.ids["a2"]]


def test_restore_rejects_assistant_row(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["a1"])


def test_restore_refuses_read_only_session(env):
    env.session.read_only = True
    with pytest.raises(PermissionError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    # untouched
    assert len(env.session.messages) == 4


def test_restore_refuses_live_turn(env, monkeypatch):
    monkeypatch.setattr(session_ops, "_live_active_stream_id", lambda s: "stream-1")
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert len(env.session.messages) == 4


def test_restore_refuses_pending_user_message(env):
    env.session.pending_user_message = ["queued prompt"]
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])


def test_durable_failure_aborts_before_sidecar_write(env, monkeypatch):
    """Dual-store fail-closed: if the durable archive raises, the sidecar must
    NOT have been rewritten (no divergent state, no false success)."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(session_ops, "_commit_rewind_via_authority", boom)
    with pytest.raises(sqlite3.OperationalError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    # in-memory transcript untouched
    assert len(env.session.messages) == 4
    # sidecar file untouched: reload from disk
    on_disk = json.loads(Path(env.session.path).read_text())
    assert len(on_disk["messages"]) == 4


def test_sidecar_publish_failure_is_fail_closed_and_self_heals(env, monkeypatch):
    """A durable commit whose sidecar publish fails must NOT report success:
    the session is flagged, the crash-safe resync marker captures the committed
    correction, and the NEXT load re-applies it — stale on-disk rows can never
    resurrect the archived suffix (the review's item #3)."""
    import api.durable_sync as durable_sync

    original_save = models.Session.save
    calls = {"n": 0}

    def flaky_save(self, *a, **k):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(models.Session, "save", flaky_save)
    with pytest.raises(RuntimeError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert calls["n"] == 2  # one retry, then fail-closed
    assert env.session.needs_state_resync is True
    marker = durable_sync.read_resync_marker(env.sid)
    assert marker and marker["kind"] == "restore"
    # The durable commit DID land (state.db is authoritative)...
    assert _active_ids(env.state_db, env.sid) == [env.ids["u1"], env.ids["a1"]]
    # ...the on-disk sidecar is stale...
    on_disk = json.loads(Path(env.session.path).read_text())
    assert len(on_disk["messages"]) == 4
    # ...and the next load re-applies the correction + clears the marker.
    monkeypatch.setattr(models.Session, "save", original_save)
    fresh = models.Session.load(env.sid)
    assert [m["content"] for m in fresh.messages] == ["first prompt", "answer 1"]
    assert durable_sync.read_resync_marker(env.sid) is None


def test_route_error_mapping_reflects_backend_contracts(env):
    """Route-level contract: PermissionError→403, ValueError→400,
    KeyError→404, durable refusals→409, generic Exception→500 (never 200)."""
    env.session.read_only = True
    with pytest.raises(PermissionError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    env.session.read_only = False
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 999999)
    with pytest.raises(KeyError):
        session_ops.restore_checkpoint_at_row_id("nosuchsession01", env.ids["u2"])
    import inspect
    src = inspect.getsource(routes)
    # The route maps these explicitly — no bare 200-on-failure path:
    assert "except PermissionError" in src
    assert "except KeyError" in src
    # 409-class refusals from the Agent's transaction guards:
    assert "except CheckpointBusyError as e:" in src
    assert "except CheckpointStaleError as e:" in src


def test_legacy_truncate_before_sink_removed(env):
    """The weaker message_id/index sink (/api/session/truncate-before) must be
    gone from the router source — durable row_id restore is the only path."""
    import inspect
    src = inspect.getsource(routes)
    assert '"/api/session/truncate-before"' not in src
    assert "checkpoint/restore" in src


def test_restore_archives_tool_and_state_only_suffix_rows(tmp_path, monkeypatch):
    """Item #6: the physical suffix can contain rows the display projection
    never surfaces (tool rows carry no display twin). The rewind must archive
    the COMPLETE durable suffix — proving the durable path does not
    reconstruct the cut from display rows."""
    from hermes_state import SessionDB

    sid = "restoretest-toolstate"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_db = tmp_path / "state.db"

    db = SessionDB(state_db)
    try:
        db.ensure_session(sid, source="webui-test")
        u1 = db.append_message(sid, "user", "first prompt")
        a1 = db.append_message(sid, "assistant", "answer 1")
        u2 = db.append_message(sid, "user", "second prompt")
        a2 = db.append_message(sid, "assistant", "answer 2")
        t1 = db.append_message(sid, "tool", None, tool_name="Bash", tool_call_id="call-1")
        u3 = db.append_message(sid, "user", "third prompt")
    finally:
        db.close()

    s = Session(session_id=sid)
    s.title = "tool/state suffix"
    # The display slice only carries visible user/assistant rows — t1 has no
    # display twin at all, yet it lives in the physical suffix.
    s.messages = [
        {"role": "user", "content": "first prompt", "id": "u1", "_row_id": u1},
        {"role": "assistant", "content": "answer 1", "id": "a1", "_row_id": a1},
        {"role": "user", "content": "second prompt", "id": "u2", "_row_id": u2},
        {"role": "assistant", "content": "answer 2", "id": "a2", "_row_id": a2},
    ]
    s.context_messages = [dict(m) for m in s.messages]

    monkeypatch.setattr(config, "SESSION_DIR", str(sessions_dir))
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda **kw: state_db)
    s.save()
    monkeypatch.setitem(models.SESSIONS, sid, s)

    res = session_ops.restore_checkpoint_at_row_id(sid, u2)
    # The display-invisible tool row is part of the archived suffix.
    assert sorted(res["archived_state_row_ids"]) == sorted([u2, a2, t1, u3])
    assert _active_ids(state_db, sid) == [u1, a1]


def test_restore_refuses_active_compression_lock(env):
    """Item #6: a live compression lock on the transcript refuses the rewind
    inside the transaction (SessionCompressionInProgressError ->
    CheckpointBusyError -> 409), nothing is written."""
    from hermes_state import SessionDB
    db = SessionDB(env.state_db)
    try:
        assert db.try_acquire_compression_lock(
            env.sid, "compressor-test", ttl_seconds=120.0) is True
    finally:
        db.close()
    with pytest.raises(session_ops.CheckpointBusyError):
        session_ops.restore_checkpoint_at_row_id(env.sid, env.ids["u2"])
    assert len(env.session.messages) == 4
    assert _active_ids(env.state_db, env.sid) == [
        env.ids["u1"], env.ids["a1"], env.ids["u2"], env.ids["a2"]]
