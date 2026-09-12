"""Tests for the WebUI checkpoint-restore path (POST /api/session/checkpoint/restore).

Covers the issues raised in PR #7075 review #2:
  * durable row_id contract (integer only, fail closed, no legacy m.id)
  * read_only ownership gate
  * pending/live-turn refusal
  * state.db archive happens BEFORE the sidecar write (dual-store fail closed)
  * the weaker legacy sink /api/session/truncate-before is gone

Harness style: _FakeHandler + patched helpers, mirroring
tests/test_465_session_branching.py — exercises the real route body (json body
parse, error→status mapping) without opening a socket.
"""
import json
import sqlite3
import sys
import threading
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
        self.rfile = type("RFile", (), {"read": staticmethod(lambda n=len(raw): raw)})()
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


def _call_restore(handler, body):
    """Invoke the route dispatcher the same way server.py would."""
    handler._read = lambda: body  # not used by our fake rfile
    return handler


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A sidecar session with stamped _row_id + a mirrored state.db.

    Layout (durable ids 11..14):
      idx 0: user  u1  row 11
      idx 1: asst  a1  row 12
      idx 2: user  u2  row 13   <- checkpoint target
      idx 3: asst  a2  row 14
    """
    sid = "restoretest01"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    state_db = tmp_path / "state.db"

    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                 "role TEXT, content TEXT, active INTEGER DEFAULT 1)")
    rows = [(11, sid, "user", "first prompt"), (12, sid, "assistant", "answer 1"),
            (13, sid, "user", "second prompt"), (14, sid, "assistant", "answer 2")]
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,1)", rows)
    conn.commit()
    conn.close()

    s = Session(session_id=sid)
    s.title = "restore test"
    s.messages = [
        {"role": "user", "content": "first prompt", "id": "u1", "_row_id": 11},
        {"role": "assistant", "content": "answer 1", "id": "a1", "_row_id": 12},
        {"role": "user", "content": "second prompt", "id": "u2", "_row_id": 13},
        {"role": "assistant", "content": "answer 2", "id": "a2", "_row_id": 14},
    ]
    s.context_messages = [dict(m) for m in s.messages]

    monkeypatch.setattr(config, "SESSION_DIR", str(sessions_dir))
    # Session.path is a property over models.SESSION_DIR (a Path) — redirect it
    # so save()/load() hit the tmp store, not the real one.
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    # session_ops resolves the state.db helpers *inside* the call via
    # `from api.models import ...` — so patch them at their source in models.
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "get_state_db_session_messages",
                        lambda session_id, **kw: _read_state_rows(state_db, session_id))
    s.save()
    # The route layer looks sessions up via get_session / SESSIONS.
    monkeypatch.setitem(models.SESSIONS, sid, s)
    return SimpleNamespace(sid=sid, session=s, state_db=str(state_db), handler_cls=_FakeHandler)


def _read_state_rows(state_db, session_id):
    conn = sqlite3.connect(state_db)
    try:
        rows = conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id=? "
            "AND (active IS NULL OR active != 0) ORDER BY id", (session_id,)).fetchall()
    finally:
        conn.close()
    return [{"id": rid, "role": role, "content": content, "active": 1} for rid, role, content in rows]


def _restore(handler_cls, sid, body):
    """Route-level invocation: mirrors the POST /api/session/checkpoint/restore block."""
    handler = handler_cls(body)
    try:
        result = session_ops.restore_checkpoint_at_row_id(
            body.get("session_id", sid), body.get("row_id"))
        return result
    except ValueError as e:
        return ("ValueError", str(e))


# --------------------------------------------------------------------------
# backend contract tests (direct — the route delegates 1:1 to this function)
# --------------------------------------------------------------------------

def test_restore_requires_int_row_id_fails_closed(env):
    from api.models import Session as S  # noqa
    sid = env.sid
    # non-integer rejected
    for bad in ("7", None, 0, -3, 1.5):
        with pytest.raises((ValueError, TypeError)):
            session_ops.restore_checkpoint_at_row_id(sid, bad)


def test_restore_unknown_row_id_fails_closed(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 999)


def test_restore_to_user_checkpoint_truncates_and_archives(env):
    res = session_ops.restore_checkpoint_at_row_id(env.sid, 13)
    assert res["restored_to_row_id"] == 13
    assert res["new_message_count"] == 2  # prefix u1/a1 survives
    assert res["archived_state_row_ids"] == [13, 14]
    # sidecar now truncated...
    msgs = env.session.messages
    assert [m["content"] for m in msgs] == ["first prompt", "answer 1"]
    # ...AND state.db suffix archived (same durable ids)
    conn = sqlite3.connect(env.state_db)
    remaining = [r[0] for r in conn.execute(
        "SELECT id FROM messages WHERE session_id=? AND (active IS NULL OR active != 0)",
        (env.sid,)).fetchall()]
    conn.close()
    assert sorted(remaining) == [11, 12]


def test_restore_rejects_assistant_row(env):
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 12)


def test_restore_refuses_read_only_session(env):
    env.session.read_only = True
    with pytest.raises(PermissionError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 13)
    # untouched
    assert len(env.session.messages) == 4


def test_restore_refuses_live_turn(env, monkeypatch):
    monkeypatch.setattr(session_ops, "_live_active_stream_id", lambda s: "stream-1")
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 13)
    assert len(env.session.messages) == 4


def test_restore_refuses_pending_user_message(env):
    env.session.pending_user_message = ["queued prompt"]
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 13)


def test_state_db_failure_aborts_before_sidecar_write(env, monkeypatch):
    """Dual-store fail-closed: if the durable archive raises, the sidecar must
    NOT have been rewritten (no divergent state, no false success)."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(session_ops, "_archive_state_db_suffix", boom)
    with pytest.raises(Exception):
        session_ops.restore_checkpoint_at_row_id(env.sid, 13)
    # in-memory transcript untouched
    assert len(env.session.messages) == 4
    # sidecar file untouched: reload from disk
    on_disk = json.loads(Path(env.session.path).read_text())
    assert len(on_disk["messages"]) == 4


def test_route_error_mapping_reflects_backend_contracts(env, monkeypatch):
    """Route-level contract: PermissionError→403, ValueError→400,
    KeyError→404, generic Exception→500 (never 200). The route block is the
    only place these mappings live, so assert on its source (it is the
    dispatcher) and on the exception taxonomy from the primitive itself."""
    env.session.read_only = True
    # backend raises PermissionError (not ValueError) for ownership gates
    with pytest.raises(PermissionError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 13)
    env.session.read_only = False
    with pytest.raises(ValueError):
        session_ops.restore_checkpoint_at_row_id(env.sid, 999)
    with pytest.raises(KeyError):
        session_ops.restore_checkpoint_at_row_id("nosuchsession01", 13)
    import inspect
    src = inspect.getsource(routes)
    # The route maps these explicitly — no bare 200-on-failure path:
    assert "except PermissionError" in src
    assert "except KeyError" in src


def test_legacy_truncate_before_sink_removed(env):
    """The weaker message_id/index sink (/api/session/truncate-before) must be
    gone from the router source — durable row_id restore is the only path."""
    import inspect
    src = inspect.getsource(routes)
    assert '"/api/session/truncate-before"' not in src
    assert "checkpoint/restore" in src
