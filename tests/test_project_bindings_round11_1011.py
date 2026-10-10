"""Round-11 re-review of PR #6836 (maintainer re-gate review 5480274813 @
2026-10-10T18:19:55Z, anchored ``142dadde``).

The one remaining finding:

    "[SILENT] ``api/routes.py:1069``: chats are filed after auto-assign is turned
    off or the workspace is detached.  The sweep checks the live binding **before**
    it waits for the session lock, and the assignments at ``:1112`` and ``:1150``
    then use that stale answer.  Verified over real HTTP: hold an unassigned
    chat's lock, enable auto-assign, let the sweep reach that lock, disable
    auto-assign (or save ``workspaces: null``) successfully, then release the
    lock.  The chat gains the project ID in the cache and on disk, even though
    the user just turned filing off.

    Fix (validated by a review-only patch that stopped both reproductions): once
    the session lock is held, take ``_PROJECTS_CATALOG_LOCK``, re-read the live
    binding, require ``auto_assign`` and that the session's actual workspace is
    still a member, and do the project-ID assignment inside that critical
    section.  Release the catalog lock before ``save()``.  Apply this to both the
    cached-streaming and the persisted-session paths.

    Please make this the rule for every place the sweep assigns a project ID,
    not just these two lines."

Fix: every project-id write now goes through ``_auto_assign_claim_session``,
which re-reads the live row and makes the assignment inside ONE
``_PROJECTS_CATALOG_LOCK`` critical section while the session's agent lock is
held (and releases it before the caller's ``save()``).

The sweep-level tests below reproduce the maintainer's repro in miniature: the
session's agent lock is held by the test while the sweep reaches it, the live
binding is flipped through the same catalog lock a real ``/api/projects/bind``
uses, and the lock is then released.  No project id may land.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class _ProbeLock:
    """RLock-shaped context manager that records whether it is currently held."""

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


class _SignallingLock:
    """The session's agent lock, signalling when the sweep reaches its wait."""

    def __init__(self, inner, entered=None):
        self._inner = inner
        self._entered = entered

    def __enter__(self):
        if self._entered is not None:
            self._entered.set()
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        return False

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()


def _project_row(pid, ws_str, **extra):
    row = {
        "project_id": pid,
        "name": "Round11",
        "profile": "default",
        "auto_assign": True,
        "workspaces": [ws_str],
    }
    row.update(extra)
    return row


def _install_sweep_stubs(monkeypatch, tmp_path, catalog, sid, ws_str, active=()):
    """Isolate the catalog, the session index, the cache and the metadata probe."""
    import api.routes as routes

    index_file = tmp_path / "_index.json"
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": sid,
                    "workspace": ws_str,
                    "profile": "default",
                    "project_id": None,
                    "active_stream_id": (list(active) or [""])[0],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: catalog)
    monkeypatch.setattr(routes, "save_projects", lambda ps: None)
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda s: "")
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set(active))
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    sessions: dict = {}
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    return routes, sessions


class _Row:
    """A session whose save() records the assignment it persisted."""

    def __init__(self, sid, ws_str, active_stream_id=""):
        self.session_id = sid
        self.project_id = None
        self.profile = "default"
        self.workspace = ws_str
        self.active_stream_id = active_stream_id
        self.save_calls: list = []

    def save(self, touch_updated_at=True):
        self.save_calls.append((self.project_id, touch_updated_at))


# ---------------------------------------------------------------------------
# The claim helper itself (behaviour + the critical section it must use)
# ---------------------------------------------------------------------------


def test_claim_session_files_when_the_live_binding_still_covers_the_workspace(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda *a, **k: [_project_row("p1", "/ws/a")],
    )
    row = _Row("sess-claim-ok", "/ws/a")
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is True
    assert row.project_id == "p1"


def test_claim_session_refuses_when_auto_assign_was_switched_off(monkeypatch):
    """The exact stale answer the finding is about: the row still exists."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda *a, **k: [_project_row("p1", "/ws/a", auto_assign=False)],
    )
    row = _Row("sess-claim-off", "/ws/a")
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is False
    assert row.project_id is None


def test_claim_session_refuses_when_the_workspace_was_detached(monkeypatch):
    """``workspaces: null`` (or a list without this workspace) must not file."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda *a, **k: [_project_row("p1", "/ws/a", workspaces=[])],
    )
    row = _Row("sess-claim-detached", "/ws/a")
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is False
    assert row.project_id is None


def test_claim_session_fails_closed_when_the_project_row_is_gone(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: [])
    row = _Row("sess-claim-gone", "/ws/a")
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is False
    assert row.project_id is None


def test_claim_session_fails_closed_when_the_catalog_cannot_be_read(monkeypatch):
    """An unreadable catalog is "unknown", and unknown may not file a chat."""
    import api.routes as routes

    def _boom(*a, **k):
        raise OSError("catalog unreadable")

    monkeypatch.setattr(routes, "load_projects", _boom)
    row = _Row("sess-claim-unknown", "/ws/a")
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is False
    assert row.project_id is None


def test_claim_session_assigns_inside_the_catalog_critical_section(monkeypatch):
    """Both the re-read and the write share ONE catalog critical section, and the
    lock is released before the caller's save() (the maintainer's wording)."""
    import api.routes as routes

    probe = _ProbeLock()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", probe)
    monkeypatch.setattr(
        routes, "load_projects", lambda *a, **k: [_project_row("p1", "/ws/a")]
    )

    class _Watched:
        def __init__(self):
            self.__dict__["project_id"] = None
            self.__dict__["assigned_while_catalog_held"] = None

        def __setattr__(self, name, value):
            if name == "project_id":
                self.__dict__["project_id"] = value
                self.__dict__["assigned_while_catalog_held"] = probe.held()
            else:
                self.__dict__[name] = value

    row = _Watched()
    assert routes._auto_assign_claim_session("p1", row, "/ws/a") is True
    assert row.project_id == "p1"
    assert row.assigned_while_catalog_held is True, (
        "the project id must be written while _PROJECTS_CATALOG_LOCK is held"
    )
    assert probe.held() is False, (
        "the catalog lock must be released before the caller saves"
    )


# ---------------------------------------------------------------------------
# The maintainer's repro, in miniature: flip the binding while the sweep waits
# on a held session lock.  Nothing may be filed.
# ---------------------------------------------------------------------------


def _run_sweep_with_a_flip(tmp_path, monkeypatch, flip):
    """Start the sweep, let it block on a held session lock, flip the binding
    under the catalog lock, release the session lock and report what happened."""
    ws = tmp_path / "ws-r11"
    ws.mkdir()
    ws_str = str(ws)
    sid = "sess-r11"
    pid = "proj_r11"
    catalog = [_project_row(pid, ws_str)]
    routes, sessions = _install_sweep_stubs(monkeypatch, tmp_path, catalog, sid, ws_str)

    row = _Row(sid, ws_str)
    monkeypatch.setattr(
        routes, "get_session", lambda s, metadata_only=False: row if s == sid else None
    )

    entered = threading.Event()
    real = routes._get_session_agent_lock(sid)
    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda s: _SignallingLock(real, entered)
    )

    assert real.acquire(timeout=5)
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(routes._apply_project_auto_assign(dict(catalog[0]))),
        daemon=True,
    )
    try:
        thread.start()
        assert entered.wait(5), "the sweep never reached its session-lock wait"
        assert thread.is_alive(), "the sweep must still be waiting for the held lock"
        # The user's Save lands while the sweep waits. The real /api/projects/bind
        # writes its row under _PROJECTS_CATALOG_LOCK, so the flip does too.
        with routes._PROJECTS_CATALOG_LOCK:
            flip(catalog)
    finally:
        real.release()
        thread.join(10)

    assert not thread.is_alive()
    return result, row, sessions, catalog


def test_sweep_files_nothing_when_auto_assign_is_switched_off_while_it_waits(
    tmp_path, monkeypatch
):
    """[SILENT] api/routes.py:1069 — the reported reproduction (persisted path)."""
    result, row, _sessions, _catalog = _run_sweep_with_a_flip(
        tmp_path, monkeypatch, lambda catalog: catalog[0].__setitem__("auto_assign", False)
    )
    assert result == [0], result
    assert row.project_id is None, "auto-assign was off before the lock was released"
    assert row.save_calls == [], "nothing may be persisted after filing was switched off"


def test_sweep_files_nothing_when_the_workspace_is_detached_while_it_waits(
    tmp_path, monkeypatch
):
    """The second half of the finding: ``workspaces: null`` mid-sweep."""
    result, row, _sessions, _catalog = _run_sweep_with_a_flip(
        tmp_path, monkeypatch, lambda catalog: catalog[0].__setitem__("workspaces", [])
    )
    assert result == [0], result
    assert row.project_id is None
    assert row.save_calls == []


def test_sweep_files_nothing_when_the_project_is_deleted_while_it_waits(
    tmp_path, monkeypatch
):
    """Deleting the row mid-sweep answers None = unknown, which fails CLOSED."""
    result, row, _sessions, _catalog = _run_sweep_with_a_flip(
        tmp_path, monkeypatch, lambda catalog: catalog.clear()
    )
    assert result == [0], result
    assert row.project_id is None
    assert row.save_calls == []


def test_sweep_still_files_when_the_binding_did_not_change(tmp_path, monkeypatch):
    """Control: the same harness files the chat when nothing is flipped."""
    result, row, _sessions, _catalog = _run_sweep_with_a_flip(
        tmp_path, monkeypatch, lambda catalog: None
    )
    assert result == [1], result
    assert row.project_id == "proj_r11"
    assert row.save_calls == [("proj_r11", False)]


def test_cached_streaming_path_also_rechecks_the_live_binding(tmp_path, monkeypatch):
    """Same repro against the :1112 (cached / actively-streaming) assignment."""
    active = "stream-r11"
    ws = tmp_path / "ws-r11-stream"
    ws.mkdir()
    ws_str = str(ws)
    sid = "sess-r11-stream"
    pid = "proj_r11_stream"
    catalog = [_project_row(pid, ws_str)]
    routes, sessions = _install_sweep_stubs(
        monkeypatch, tmp_path, catalog, sid, ws_str, active={active}
    )
    cached = _Row(sid, ws_str, active_stream_id=active)
    sessions[sid] = cached
    # The authoritative fall-through must refuse for the same reason.
    monkeypatch.setattr(
        routes, "get_session", lambda s, metadata_only=False: cached if s == sid else None
    )

    entered = threading.Event()
    real = routes._get_session_agent_lock(sid)
    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda s: _SignallingLock(real, entered)
    )

    assert real.acquire(timeout=5)
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(routes._apply_project_auto_assign(dict(catalog[0]))),
        daemon=True,
    )
    try:
        thread.start()
        assert entered.wait(5), "the sweep never reached its session-lock wait"
        assert thread.is_alive()
        with routes._PROJECTS_CATALOG_LOCK:
            catalog[0]["auto_assign"] = False
    finally:
        real.release()
        thread.join(10)

    assert not thread.is_alive()
    assert result == [0], result
    assert cached.project_id is None, (
        "the cached/streaming assignment at api/routes.py:1112 used the stale answer"
    )
    assert cached.save_calls == []


# ---------------------------------------------------------------------------
# Source guards: the rule, not just these two lines
# ---------------------------------------------------------------------------


def _sweep_source() -> str:
    src = _read("api/routes.py")
    return src[
        src.index("def _auto_assign_sweep_body(") : src.index(
            "def _auto_assign_candidate_count("
        )
    ]


def test_source_no_sweep_assignment_bypasses_the_claim_helper():
    seg = _sweep_source()
    assert ".project_id = pid" not in seg, (
        "the sweep must not write a project id directly - every assignment has "
        "to re-read the live binding under _PROJECTS_CATALOG_LOCK"
    )
    assert seg.count("_auto_assign_claim_session(") == 2, (
        "both assignment sites (cached-streaming and persisted) must go through "
        "the claim helper"
    )


def test_source_the_claim_helper_writes_inside_the_catalog_lock_and_releases():
    src = _read("api/routes.py")
    seg = src[
        src.index("def _auto_assign_claim_session(") : src.index(
            "def _auto_assign_sweep_body("
        )
    ]
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
    assign_i = next(
        k for k, line in enumerate(lines) if ".project_id = project_id" in line
    )
    ret_i = next(k for k, line in enumerate(lines) if line.strip() == "return True")

    assert with_i < assign_i < block_end, (
        "the assignment must happen inside the catalog critical section"
    )
    assert ret_i >= block_end, (
        "the catalog lock must be released before this returns (the caller saves "
        "afterwards, and full-history I/O may not run behind the shared lock)"
    )
    assert (len(lines[ret_i]) - len(lines[ret_i].lstrip())) == indent, (
        "return True must sit OUTSIDE the with-block, so the lock is released "
        "before the caller's save()"
    )
    # Only ONE acquisition site: the outer block above plus the re-entrant
    # reader call, never a bare load_projects() outside the lock.
    assert "load_projects()" not in seg, (
        "the helper must read the live binding through _auto_assign_live_binding"
    )


def test_changed_module_compiles():
    compile(_read("api/routes.py"), "api/routes.py", "exec")
