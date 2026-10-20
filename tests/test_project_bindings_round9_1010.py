"""Round-9 re-review of the merge-pushed head ``abd2dece`` (maintainer re-gate
review 5479525945 @ 2026-10-10T15:11:33Z, CORE finding api/routes.py:19906).

"[CORE] api/routes.py:19906 — project deletion blocks ordinary New Chat requests
because ``_clear_cached_sessions_for_project()`` waits up to five seconds per
session while holding ``_PROJECTS_CATALOG_LOCK``. Verified over real HTTP: seven
held session locks caused New Chat to time out after 30.03 seconds; master
completed immediately. Exact fix: move the ``_clear_cached_sessions_for_project(
...)`` call to just after the ``with _PROJECTS_CATALOG_LOCK:`` block. Keep the
catalog removal serialized, keep its per-session locks and the existing
write-through outside the catalog lock."

The fix moves the CLEAR out of the catalog-lock block; the row removal
(``save_projects(projects)``) stays inside it. The mutual exclusion the old
placement provided is unchanged, because it is the ROW REMOVAL that is
serialized with the paths that publish a ``project_id`` (an explicit id on
``/api/session/new``, and ``/api/session/move``): a session either already
published its ``project_id`` when the scan runs (so the scan clears it) or it
validates against the catalog after the removal and stays unassigned.

The tests below reproduce the symptom directly: while the delete is inside the
clear (waiting for a busy session's agent lock), another thread — the one a New
Chat request would run on — must be able to take the catalog lock, and the
clear's own ``acquire()`` must not observe it held.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]

_DELETE_PATH = "/api/projects/delete"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class _ProbeLock:
    """Context-manager lock that records whether it is currently held."""

    def __init__(self):
        self._inner = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self._inner.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self._inner.release()
        return False

    def acquire(self, *args, **kwargs):
        got = self._inner.acquire(*args, **kwargs)
        if got:
            self.depth += 1
        return got

    def release(self):
        self.depth -= 1
        self._inner.release()

    def held(self) -> bool:
        return self.depth > 0


class _CatalogWatchingSessionLock:
    """The session's agent lock, recording the catalog-lock state on acquire.

    ``_clear_cached_sessions_for_project`` takes this lock per target session
    with a bounded wait, so the state recorded here IS the state the delete
    request waits in.
    """

    def __init__(self, inner, catalog_probe, entered=None):
        self._inner = inner
        self._catalog = catalog_probe
        self._entered = entered
        self.catalog_held_on_acquire: list = []

    def acquire(self, *args, **kwargs):
        self.catalog_held_on_acquire.append(self._catalog.held())
        if self._entered is not None:
            self._entered.set()
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()


class _CachedRow:
    """A cached session whose ``save()`` writes the sidecar."""

    def __init__(self, sid, project_id, sidecar, locks, catalog_probe=None):
        self.session_id = sid
        self.project_id = project_id
        self.profile = "default"
        self.workspace = "/ws/round9"
        self.active_stream_id = ""
        self._sidecar = sidecar
        self._locks = locks
        self._catalog_probe = catalog_probe
        self.save_calls = []

    def save(self, touch_updated_at=True):
        self.save_calls.append(
            (
                self.project_id,
                touch_updated_at,
                None if self._catalog_probe is None else self._catalog_probe.held(),
            )
        )
        if self._sidecar is not None:
            self._sidecar.write_text(
                json.dumps(
                    {"session_id": self.session_id, "project_id": self.project_id}
                ),
                encoding="utf-8",
            )


def _post_delete(monkeypatch, body, responses):
    """Drive /api/projects/delete in-process; each response lands in ``responses``."""
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
        SimpleNamespace(command="POST"), SimpleNamespace(path=_DELETE_PATH)
    )


def _install_delete_stubs(monkeypatch, tmp_path, pid, sid, session_dir, catalog):
    """Isolate the catalog, the session cache and the session lock factory."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda *a, **k: [
            {"project_id": pid, "name": "Gone", "profile": "default", "workspaces": []}
        ],
    )
    monkeypatch.setattr(routes, "save_projects", lambda ps: None)
    # No index: the index pass cannot be what rewrites the sidecar here.
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "missing_index.json")
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    sessions: OrderedDict = OrderedDict()
    locks: dict = {}
    real_lock_factory = routes._get_session_agent_lock
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        routes,
        "_get_session_agent_lock",
        lambda s: locks.setdefault(s, real_lock_factory(s)),
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", catalog)
    # The write-through resolves each target by loading a FRESH sidecar through
    # ``Session.load`` (a stale FULL cache entry must not be saved back over a
    # newer sidecar — maintainer re-gates 2026-10-10T23:49:54Z and
    # 2026-10-11T02:08:20Z). This fixture keeps its fake rows in ``sessions``
    # and its sidecars are JSON stubs, so the loader answers from there: a real
    # load would go to disk and bypass the row's recorded save() calls.
    import api.models as models

    class _FakeSession:
        @staticmethod
        def load(sid):
            return sessions.get(sid)

    monkeypatch.setattr(models, "Session", _FakeSession)
    return routes, sessions, locks


# ---------------------------------------------------------------------------
# The CORE finding: the clear must NOT wait for session locks behind the
# catalog lock (New Chat / workspace edits need that lock).
# ---------------------------------------------------------------------------


def test_delete_clear_runs_outside_the_catalog_lock(tmp_path, monkeypatch):
    """The clear's per-session lock waits must not hold the catalog lock."""
    routes, sessions, locks = _install_delete_stubs(
        monkeypatch, tmp_path, "proj_round9_clear", "sess-round9-clear", tmp_path, None
    )
    catalog = _ProbeLock()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", catalog)

    pid, sid = "proj_round9_clear", "sess-round9-clear"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "project_id": pid}), encoding="utf-8"
    )

    inner = threading.RLock()
    probe_lock = _CatalogWatchingSessionLock(inner, catalog)
    locks[sid] = probe_lock
    row = _CachedRow(sid, pid, sidecar, locks, catalog_probe=catalog)
    sessions[sid] = row

    responses = []
    assert _post_delete(monkeypatch, {"project_id": pid}, responses) is True
    assert [r["status"] for r in responses] == [200], responses

    # The clear really happened: the live row lost the id and the sidecar was
    # rewritten on disk (outside the catalog lock, as before).
    assert row.project_id is None
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None
    assert row.save_calls == [(None, False, False)], row.save_calls

    # ...and neither lock wait saw the catalog lock held.
    assert probe_lock.catalog_held_on_acquire, (
        "the delete never took the session's agent lock"
    )
    assert probe_lock.catalog_held_on_acquire[0] is False, (
        "the delete's clear waited for a session lock while holding "
        "_PROJECTS_CATALOG_LOCK (New Chat / workspace edits block behind it)"
    )
    assert not any(probe_lock.catalog_held_on_acquire)


def test_new_chat_can_take_the_catalog_lock_while_a_delete_waits(
    tmp_path, monkeypatch
):
    """A New Chat request must not stall behind a delete's session-lock wait.

    Reproduces the maintainer's repro in miniature: one session's agent lock is
    held (as a streaming save would), so the delete sits inside the clear for
    its whole bounded wait. From another thread — the request thread a New Chat
    would run on — the catalog lock must still be acquirable.
    """
    routes, sessions, locks = _install_delete_stubs(
        monkeypatch, tmp_path, "proj_round9_busy", "sess-round9-busy", tmp_path, None
    )
    catalog = _ProbeLock()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", catalog)

    pid, sid = "proj_round9_busy", "sess-round9-busy"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    entered = threading.Event()
    # 1 s per busy session keeps the delete inside the clear long enough to
    # observe, and well under the 5 s production budget.
    monkeypatch.setattr(routes, "_CLEAR_CACHED_SESSION_LOCK_TIMEOUT", 1.0)

    real = routes._get_session_agent_lock(sid)
    probe_lock = _CatalogWatchingSessionLock(real, catalog, entered=entered)
    locks[sid] = probe_lock
    row = _CachedRow(sid, pid, None, locks, catalog_probe=catalog)
    sessions[sid] = row

    # A busy session: its own agent lock is taken before the delete runs.
    assert real.acquire(timeout=5)
    responses = []
    delete_thread = threading.Thread(
        target=lambda: _post_delete(monkeypatch, {"project_id": pid}, responses),
        daemon=True,
    )
    acquired_while_waiting = False
    try:
        delete_thread.start()
        assert entered.wait(5), "the delete never reached its session-lock wait"
        assert delete_thread.is_alive(), "the delete did not wait for the busy session"
        if catalog.acquire(timeout=0.5):
            catalog.release()
            acquired_while_waiting = True
    finally:
        real.release()
        delete_thread.join(10)

    assert acquired_while_waiting, (
        "New Chat could not take _PROJECTS_CATALOG_LOCK while the delete waited "
        "for a busy session's lock"
    )
    assert probe_lock.catalog_held_on_acquire[0] is False
    assert not delete_thread.is_alive()


# ---------------------------------------------------------------------------
# Source guard: the move is exactly one block, and the row removal stays inside
# the catalog lock (the ordering guarantee the old placement provided).
# ---------------------------------------------------------------------------


def _delete_handler_source() -> str:
    src = _read("api/routes.py")
    i = src.index('parsed.path == "/api/projects/delete"')
    j = src.index("if parsed.path ==", i + 10)
    return src[i:j]


def test_source_the_clear_is_after_the_catalog_lock_block():
    """``_clear_cached_sessions_for_project`` is called with the lock released."""
    seg = _delete_handler_source()
    lines = seg.splitlines()
    with_i = next(
        k for k, line in enumerate(lines) if line.strip() == "with _PROJECTS_CATALOG_LOCK:"
    )
    indent = len(lines[with_i]) - len(lines[with_i].lstrip())
    block_end = next(
        k
        for k in range(with_i + 1, len(lines))
        if lines[k].strip() and (len(lines[k]) - len(lines[k].lstrip())) <= indent
    )
    save_i = next(k for k, line in enumerate(lines) if "save_projects(projects)" in line)
    clear_i = next(
        k
        for k, line in enumerate(lines)
        if "_clear_cached_sessions_for_project(" in line
    )

    # The catalog removal is still serialized by the shared lock...
    assert with_i < save_i < block_end, (with_i, save_i, block_end)
    # ...and the clear is not inside that block any more.
    assert clear_i > block_end, (
        "the clear is still called inside the _PROJECTS_CATALOG_LOCK block "
        "(it would stall New Chat / workspace edits)"
    )
    # The write-through stays outside the lock too (Greptile P1 12:41:25Z).
    persist_i = next(
        k for k, line in enumerate(lines) if "_persist_cleared_project_ids(" in line
    )
    assert persist_i > clear_i


def test_changed_module_compiles():
    compile(_read("api/routes.py"), "api/routes.py", "exec")
