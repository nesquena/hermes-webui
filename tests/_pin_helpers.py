"""Shared fakes for the state.db pin tests."""

import sqlite3
import sys
import threading
from types import SimpleNamespace


def db_pins(path):
    conn = sqlite3.connect(str(path))
    try:
        return {row[0]: bool(row[1]) for row in conn.execute("SELECT id, pinned FROM sessions")}
    finally:
        conn.close()


class SqliteSessionDB:
    """Minimal ``hermes_state.SessionDB`` over a real sqlite file."""

    fail_writes = False

    def __init__(self, db_path):
        self._conn = sqlite3.connect(str(db_path))

    def get_session(self, sid):
        row = self._conn.execute("SELECT id, pinned FROM sessions WHERE id = ?", (sid,)).fetchone()
        return {"id": row[0], "pinned": row[1]} if row else None

    def set_session_pinned(self, sid, pinned):
        if SqliteSessionDB.fail_writes:
            return False
        cur = self._conn.execute("UPDATE sessions SET pinned = ? WHERE id = ?", (int(pinned), sid))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self):
        self._conn.close()


def install_sqlite_session_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=SqliteSessionDB))
    SqliteSessionDB.fail_writes = False


class PinSess:
    archived = False

    def __init__(self, sid, profile="default", on_save=None):
        self.session_id, self.profile, self.pinned, self._on_save = sid, profile, False, on_save

    def compact(self):
        return {"session_id": self.session_id, "pinned": bool(self.pinned), "profile": self.profile}

    def save(self, touch_updated_at=True):
        if self._on_save:
            self._on_save(self)


def patch_pin_endpoint(monkeypatch, sessions, *, limit=3, persisted=(), cache=None, lock=None):
    """Stub ``/api/session/pin``'s collaborators; returns ``post(sid, pinned)`` -> ``(status, payload)``.

    Responses are recorded per thread, so concurrent requests may each call ``post``.
    """
    from api import routes

    locks, bodies, responses = {}, {}, {}
    get = lambda sid, **kw: sessions[sid]  # noqa: E731
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: lock or locks.setdefault(sid, threading.RLock()))
    monkeypatch.setattr(routes, "_get_or_materialize_session", get)
    monkeypatch.setattr(routes, "get_session", get)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, s: s)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "all_sessions", lambda *a, **kw: [dict(r) for r in persisted])
    monkeypatch.setattr(routes, "SESSIONS", {} if cache is None else cache)
    monkeypatch.setattr(routes, "_PIN_QUOTA_RESERVATIONS", {})
    monkeypatch.setattr(routes, "_PIN_QUOTA_COMMIT_SEQ", 0)
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": limit})
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: bodies[threading.current_thread().name])

    def reply(status, payload):
        responses[threading.current_thread().name] = (status, payload)
        return True

    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, extra_headers=None: reply(status, payload))
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: reply(status, {"error": msg}))

    def post(sid, pinned=True):
        name = threading.current_thread().name
        bodies[name] = {"session_id": sid, "pinned": pinned}
        routes.handle_post(object(), SimpleNamespace(path="/api/session/pin"))
        return responses[name]

    return post
