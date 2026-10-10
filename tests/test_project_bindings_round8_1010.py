"""Round-8 re-review of the merge-pushed heads (Greptile 2026-10-10T12:11:07Z).

Two P1 findings, both in the project-bindings backend of ``api/routes.py``.

1. "Preview errors skip confirmation" — ``_auto_assign_candidate_count``
   answered ``0`` when the session index was missing or unreadable. The bind
   dialog reads a definite 0 as "a sweep would file nothing" and CACHES that
   answer for the workspace snapshot it covered (``_aaConfirmedKey``), while the
   background sweep re-reads the index when it actually runs: an index that was
   absent at preview time and rebuilt before Save let the sweep file every
   existing chat with no confirmation at all. Fixed by answering ``None``
   (unknown) so the dialog keeps its existing unknown-count confirmation
   (``pb_auto_assign_confirm_unknown``).

2. "Deleted project survives on disk" — ``_clear_cached_sessions_for_project``
   cleared ``project_id`` on live cached sessions without taking their
   per-session agent lock and without writing the clear through, so a ``save()``
   that had already serialized the old ``project_id`` could still land its file
   (and its index row) after the scan and leave the deleted id on disk — the
   broken association then came back on reload. Fixed by clearing — and, for a
   session that already has a sidecar, re-saving — under that session's own
   lock, the lock every "mutate + save" pair takes.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_PREVIEW_PATH = "/api/projects/auto-assign-preview"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _dialog_source() -> str:
    """The bindings-dialog slice of ``static/sessions.js``."""
    src = _read("static/sessions.js")
    start = src.index("// Ticking the box files EVERY existing chat")
    end = src.index("\n  _seedWsList();", start)
    return src[start:end]


def _post_project_route(monkeypatch, path, body, responses):
    """Drive a project route in-process; each response lands in ``responses``."""
    import api.routes as routes

    def _record(payload, status):
        responses.append({"payload": payload, "status": status})
        return True

    monkeypatch.setattr(routes, "read_body", lambda handler: dict(body))
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None, **kw: _record(
            payload, status
        ),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: _record({"error": msg}, status),
    )
    return routes.handle_post(
        SimpleNamespace(command="POST"), SimpleNamespace(path=path)
    )


# ---------------------------------------------------------------------------
# 1 — an unreadable index is "unknown", never "nothing to file"
# ---------------------------------------------------------------------------


def test_preview_without_a_readable_index_answers_unknown(tmp_path, monkeypatch):
    """``None`` (unknown), so the dialog confirms with the unknown-count copy."""
    import api.routes as routes

    ws = tmp_path / "ws-round8-unknown"
    ws.mkdir()
    ws_str = str(ws)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    # A missing index must NOT be reported as a definite 0: the dialog caches a
    # 0 as "the sweep would file nothing" and skips the confirmation, while the
    # sweep re-reads the index once it runs.
    monkeypatch.setattr(
        routes, "SESSION_INDEX_FILE", tmp_path / "no-such-index.json"
    )
    assert routes._auto_assign_candidate_count([ws_str], "default") is None

    # An unparseable (torn) index is the same answer as a missing one...
    torn = tmp_path / "_index.json"
    torn.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", torn)
    assert routes._auto_assign_candidate_count([ws_str], "default") is None

    # ...and the route exposes it as JSON null, which is what the client keys
    # its unknown-count confirmation on (never a "0 chats" skip).
    responses = []
    assert _post_project_route(
        monkeypatch, _PREVIEW_PATH, {"workspaces": [ws_str]}, responses
    ) is True
    assert [r["status"] for r in responses] == [200], responses
    assert responses[0]["payload"] == {"count": None}, responses

    # The client half of the contract: null routes to the unknown-count prompt.
    dialog = _dialog_source()
    assert "typeof res.count==='number'" in dialog
    assert "count===null" in dialog
    assert "pb_auto_assign_confirm_unknown" in dialog

    # Control: a readable index still answers the real number.
    good = tmp_path / "_index-good.json"
    good.write_text(
        json.dumps(
            [
                {
                    "session_id": "u1",
                    "workspace": ws_str,
                    "profile": "default",
                    "project_id": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", good)
    assert routes._auto_assign_candidate_count([ws_str], "default") == 1


def test_preview_without_bound_workspaces_is_still_a_definite_zero(tmp_path, monkeypatch):
    """Nothing can be filed without a bound workspace — that 0 needs no index."""
    import api.routes as routes

    monkeypatch.setattr(
        routes, "SESSION_INDEX_FILE", tmp_path / "no-such-index.json"
    )
    assert routes._auto_assign_candidate_count([], "default") == 0
    assert routes._auto_assign_candidate_count(None, "default") == 0


# ---------------------------------------------------------------------------
# 2 — the delete's cache clear is ordered with a concurrent save and persisted
# ---------------------------------------------------------------------------


def _install_clear_stubs(monkeypatch, session_dir):
    """Isolate ``SESSIONS`` / ``SESSION_DIR`` and track the session locks."""
    import api.routes as routes

    sessions: OrderedDict = OrderedDict()
    locks: dict = {}
    real_lock_factory = routes._get_session_agent_lock
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        routes,
        "_get_session_agent_lock",
        lambda sid: locks.setdefault(sid, real_lock_factory(sid)),
    )
    return routes, sessions, locks


class _CachedRow:
    """A cached session whose ``save()`` records the lock state it saw."""

    def __init__(self, sid, project_id, sidecar: Path | None, locks, order=None):
        self.session_id = sid
        self.project_id = project_id
        self.profile = "default"
        self.workspace = "/ws/round8"
        self._sidecar = sidecar
        self._locks = locks
        self._order = order
        self.save_calls = []

    def save(self, touch_updated_at=True):
        lock_held = not self._locks[self.session_id].acquire(blocking=False)
        if not lock_held:
            self._locks[self.session_id].release()
        self.save_calls.append((self.project_id, touch_updated_at, lock_held))
        if self._order is not None:
            self._order.append("clear-save")
        if self._sidecar is not None:
            self._sidecar.write_text(
                json.dumps(
                    {"session_id": self.session_id, "project_id": self.project_id}
                ),
                encoding="utf-8",
            )


def test_delete_clear_writes_through_under_the_session_lock(tmp_path, monkeypatch):
    """A session already on disk is re-saved, under its own agent lock."""
    pid = "proj_round8_persist"
    sid = "sess-round8-persist"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "project_id": pid}), encoding="utf-8"
    )

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, sidecar, locks)
    sessions[sid] = row

    assert routes._clear_cached_sessions_for_project(pid) == 1
    assert row.project_id is None
    # Written through with the clear, while the session lock was held (the last
    # flag), and without re-dating the chat (touch_updated_at False).
    assert row.save_calls == [(None, False, True)], row.save_calls
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None


def test_delete_clear_waits_for_an_inflight_save_then_wins(tmp_path, monkeypatch):
    """The clear lands AFTER the in-flight save, never inside its payload."""
    pid = "proj_round8_inflight"
    sid = "sess-round8-inflight"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "project_id": pid}), encoding="utf-8"
    )

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    order: list[str] = []
    row = _CachedRow(sid, pid, sidecar, locks, order=order)
    sessions[sid] = row

    entered = threading.Event()
    release = threading.Event()

    def _in_flight_save():
        with locks.setdefault(sid, routes._get_session_agent_lock(sid)):
            order.append("save-start")
            entered.set()
            release.wait(5)
            order.append("save-end")

    holder = threading.Thread(target=_in_flight_save, daemon=True)
    holder.start()
    try:
        assert entered.wait(5), "the in-flight save never took the session lock"
        threading.Timer(0.2, release.set).start()
        assert routes._clear_cached_sessions_for_project(pid) == 1
    finally:
        release.set()
        holder.join(5)

    # The save finished under its own lock first; only then did the delete's
    # clear + re-save run, so nothing can put the deleted id back afterwards.
    assert order == ["save-start", "save-end", "clear-save"], order
    assert row.project_id is None
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None


def test_delete_clear_never_materializes_a_cache_only_draft(tmp_path, monkeypatch):
    """A "+ New Chat" draft (no sidecar) is cleared in the cache, not written."""
    pid = "proj_round8_draft"
    sid = "sess-round8-draft"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, None, locks)
    sessions[sid] = row

    assert routes._clear_cached_sessions_for_project(pid) == 1
    assert row.project_id is None
    assert row.save_calls == [], row.save_calls
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_clear_leaves_unrelated_sessions_alone(tmp_path, monkeypatch):
    """Only the deleted project's sessions are touched."""
    pid = "proj_round8_scope"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    other = _CachedRow("sess-round8-other", "proj_keep", None, {})

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    rows = {
        "sess-round8-a": _CachedRow("sess-round8-a", pid, None, locks),
        "sess-round8-b": _CachedRow("sess-round8-b", None, None, locks),
    }
    other._locks = locks
    rows["sess-round8-other"] = other
    sessions.update(rows)

    assert routes._clear_cached_sessions_for_project(pid) == 1
    assert rows["sess-round8-a"].project_id is None
    assert rows["sess-round8-b"].project_id is None
    assert other.project_id == "proj_keep"
    assert not other.save_calls


def test_delete_clear_is_a_noop_without_a_project_id(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    sessions["sess-round8-x"] = _CachedRow("sess-round8-x", "proj_x", None, locks)

    assert routes._clear_cached_sessions_for_project(None) == 0
    assert routes._clear_cached_sessions_for_project("") == 0
    assert sessions["sess-round8-x"].project_id == "proj_x"


@pytest.mark.parametrize("sid", ["sess-round8-busy"])
def test_delete_clear_skips_a_session_it_cannot_lock(tmp_path, monkeypatch, sid):
    """A busy session is skipped (bounded wait), never written unlocked.

    The index pass in the delete handler is its safety net, so the clear must
    not fire without the lock — that is exactly the interleaving the finding
    was about.
    """
    pid = "proj_round8_busy"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "project_id": pid}), encoding="utf-8"
    )

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, sidecar, locks)
    sessions[sid] = row

    locked = locks.setdefault(sid, routes._get_session_agent_lock(sid))
    assert locked.acquire(timeout=5)
    try:
        # A save that outlives the (shrunk) bounded acquire.
        assert routes._clear_cached_sessions_for_project(pid, lock_timeout=0.05) == 0
    finally:
        locked.release()

    assert row.project_id == pid, "a locked session must not be cleared unlocked"
    assert row.save_calls == []
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] == pid

    # Once the lock is free the same call clears it (nothing is lost forever).
    assert routes._clear_cached_sessions_for_project(pid, lock_timeout=0.05) == 1
    assert row.project_id is None
    assert row.save_calls == [(None, False, True)], row.save_calls
