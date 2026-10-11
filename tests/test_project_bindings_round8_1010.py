"""Round-8 re-review of the merge-pushed heads (Greptile P1s 2026-10-10T12:11:07Z,
2026-10-10T12:41:25Z and 2026-10-10T13:00:52Z).

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
   broken association then came back on reload. Fixed by clearing under that
   session's own lock, the lock every "mutate + save" pair takes.

3. "Deleting blocks other requests" — that write-through then ran while deletion
   held ``_PROJECTS_CATALOG_LOCK``, so deleting a project with large cached
   chats stalled New Chat and every workspace edit behind full-history writes.
   The disk half now lives in ``_persist_cleared_project_ids``, which the handler
   calls AFTER releasing the catalog lock (each write still holding the session's
   own lock, so the ordering guarantee is unchanged).

4. "Saved project settings disappear" — ``ensure_cron_project`` /
   ``ensure_webhook_project`` load and rewrite ``projects.json`` from background
   scans under their own locks, so a scan that read first and saved last erased a
   binding the user had just saved. The catalog lock now lives in the catalog
   layer (``api/models.py: PROJECTS_CATALOG_LOCK``) and both ensure_* pairs take
   it; ``api/routes.py`` keeps ``_PROJECTS_CATALOG_LOCK`` as an alias of the SAME
   object.
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
_DELETE_PATH = "/api/projects/delete"


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _dialog_source() -> str:
    """The bindings-dialog slice of ``static/sessions.js``."""
    src = _read("static/sessions.js")
    start = src.index("// Ticking the box files EVERY existing chat")
    end = src.index("\n  _seedWsList();", start)
    return src[start:end]


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
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "no-such-index.json")
    assert routes._auto_assign_candidate_count([ws_str], "default") is None

    # An unparseable (torn) index is the same answer as a missing one...
    torn = tmp_path / "_index.json"
    torn.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", torn)
    assert routes._auto_assign_candidate_count([ws_str], "default") is None

    # ...and the route exposes it as JSON null, which is what the client keys its
    # unknown-count confirmation on (never a "0 chats" skip).
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


def test_preview_without_bound_workspaces_is_still_a_definite_zero(
    tmp_path, monkeypatch
):
    """Nothing can be filed without a bound workspace — that 0 needs no index."""
    import api.routes as routes

    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "no-such-index.json")
    assert routes._auto_assign_candidate_count([], "default") == 0
    assert routes._auto_assign_candidate_count(None, "default") == 0


# ---------------------------------------------------------------------------
# 2/3 — the delete's clear is ordered with a concurrent save, and its disk half
#       runs OUTSIDE the catalog lock
# ---------------------------------------------------------------------------


class _CachedRow:
    """A cached session whose ``save()`` records the lock state it saw."""

    def __init__(
        self,
        sid,
        project_id,
        sidecar: Path | None,
        locks,
        order=None,
        catalog_probe=None,
        active_stream_id=None,
    ):
        self.session_id = sid
        self.project_id = project_id
        self.profile = "default"
        self.workspace = "/ws/round8"
        self.active_stream_id = active_stream_id
        self._sidecar = sidecar
        self._locks = locks
        self._order = order
        self._catalog_probe = catalog_probe
        self.save_calls = []

    def save(self, touch_updated_at=True):
        lock_held = not self._locks[self.session_id].acquire(blocking=False)
        if not lock_held:
            self._locks[self.session_id].release()
        self.save_calls.append(
            (
                self.project_id,
                touch_updated_at,
                lock_held,
                None if self._catalog_probe is None else self._catalog_probe.held(),
            )
        )
        if self._order is not None:
            self._order.append("clear-save")
        if self._sidecar is not None:
            self._sidecar.write_text(
                json.dumps(
                    {"session_id": self.session_id, "project_id": self.project_id}
                ),
                encoding="utf-8",
            )


def _install_clear_stubs(monkeypatch, session_dir, catalog_probe=None):
    """Isolate ``SESSIONS`` / ``SESSION_DIR`` / locks and the stream probe."""
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
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    # The write-through resolves each id through the canonical freshness path
    # (`get_session`) so a stale full cache entry cannot be saved over a newer
    # sidecar (maintainer re-gate 2026-10-10T23:49:54Z). These fixtures keep
    # their fake rows in `sessions`, so the resolver must answer from there:
    # a real load would go to disk and bypass the row's recorded save() calls.
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: sessions.get(sid),
    )
    if catalog_probe is not None:
        monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", catalog_probe)
    return routes, sessions, locks


def _sidecar_for(session_dir: Path, sid: str, project_id) -> Path:
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps({"session_id": sid, "project_id": project_id}), encoding="utf-8"
    )
    return path


def test_delete_clear_collects_ids_without_writing(tmp_path, monkeypatch):
    """The ordered clear stays cache-only; the disk half is a separate call."""
    pid = "proj_round8_persist"
    sid = "sess-round8-persist"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, None, locks)
    sessions[sid] = row

    cleared_ids: list = []
    assert routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
    assert cleared_ids == [sid]
    assert row.project_id is None
    assert row.save_calls == [], "the clear must not write while the lock is held"


def test_delete_persist_writes_through_under_the_session_lock(tmp_path, monkeypatch):
    """The sidecar is rewritten, under the session lock, without the catalog one."""
    pid = "proj_round8_write"
    sid = "sess-round8-write"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = _sidecar_for(session_dir, sid, pid)

    catalog = _ProbeLock()
    routes, sessions, locks = _install_clear_stubs(
        monkeypatch, session_dir, catalog_probe=catalog
    )
    row = _CachedRow(sid, pid, sidecar, locks, catalog_probe=catalog)
    sessions[sid] = row

    cleared_ids: list = []
    with catalog:
        assert (
            routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
        )
    assert routes._persist_cleared_project_ids(pid, cleared_ids) == 1

    # (project_id, touch_updated_at, session lock held, catalog lock held)
    assert row.save_calls == [(None, False, True, False)], row.save_calls
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None


def test_delete_persist_waits_for_an_inflight_save_then_wins(tmp_path, monkeypatch):
    """The write-through lands AFTER the in-flight save, never inside it."""
    pid = "proj_round8_inflight"
    sid = "sess-round8-inflight"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = _sidecar_for(session_dir, sid, pid)

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
        cleared_ids: list = []
        threading.Timer(0.2, release.set).start()
        assert (
            routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
        )
        assert routes._persist_cleared_project_ids(pid, cleared_ids) == 1
    finally:
        release.set()
        holder.join(5)

    # The save finished under its own lock first; only then did the unlink write
    # run, so nothing can put the deleted id back afterwards.
    assert order == ["save-start", "save-end", "clear-save"], order
    assert row.project_id is None
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None


def test_delete_persist_never_materializes_a_cache_only_draft(tmp_path, monkeypatch):
    """A "+ New Chat" draft (no sidecar) is cleared in the cache, not written."""
    pid = "proj_round8_draft"
    sid = "sess-round8-draft"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, None, locks)
    sessions[sid] = row

    cleared_ids: list = []
    assert routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
    assert row.project_id is None
    assert routes._persist_cleared_project_ids(pid, cleared_ids) == 0
    assert row.save_calls == [], row.save_calls
    assert not (session_dir / f"{sid}.json").exists()


def test_delete_persist_defers_to_a_streaming_worker(tmp_path, monkeypatch):
    """An actively streaming session is skipped (its worker persists the clear)."""
    pid = "proj_round8_stream"
    sid = "sess-round8-stream"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = _sidecar_for(session_dir, sid, pid)

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"stream-1"})
    row = _CachedRow(sid, pid, sidecar, locks, active_stream_id="stream-1")
    sessions[sid] = row

    cleared_ids: list = []
    assert routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
    assert row.project_id is None
    assert routes._persist_cleared_project_ids(pid, cleared_ids) == 0
    assert row.save_calls == [], row.save_calls
    # The live object carries the clear, so the worker's next save writes it.
    row.save()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None


def test_delete_persist_leaves_a_row_refiled_under_another_project(
    tmp_path, monkeypatch
):
    """A row re-filed while we waited must not be un-filed by the delete."""
    pid = "proj_round8_refiled"
    sid = "sess-round8-refiled"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = _sidecar_for(session_dir, sid, pid)

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, sidecar, locks)
    sessions[sid] = row

    cleared_ids: list = []
    assert routes._clear_cached_sessions_for_project(pid, cleared_ids=cleared_ids) == 1
    row.project_id = "proj_other"  # the user re-filed it meanwhile
    assert routes._persist_cleared_project_ids(pid, cleared_ids) == 0
    assert row.save_calls == []
    assert row.project_id == "proj_other"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] == pid


def test_delete_clear_skips_a_session_it_cannot_lock(tmp_path, monkeypatch):
    """A busy session is skipped (bounded wait), never written unlocked."""
    pid = "proj_round8_busy"
    sid = "sess-round8-busy"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()

    routes, sessions, locks = _install_clear_stubs(monkeypatch, session_dir)
    row = _CachedRow(sid, pid, None, locks)
    sessions[sid] = row

    locked = locks.setdefault(sid, routes._get_session_agent_lock(sid))
    assert locked.acquire(timeout=5)
    try:
        cleared_ids: list = []
        assert (
            routes._clear_cached_sessions_for_project(
                pid, lock_timeout=0.05, cleared_ids=cleared_ids
            )
            == 0
        )
        assert cleared_ids == []
    finally:
        locked.release()

    assert row.project_id == pid, "a locked session must not be cleared unlocked"

    # Once the lock is free the same call clears it (nothing is lost forever).
    cleared_ids = []
    assert (
        routes._clear_cached_sessions_for_project(
            pid, lock_timeout=0.05, cleared_ids=cleared_ids
        )
        == 1
    )
    assert cleared_ids == [sid]
    assert row.project_id is None


def test_delete_endpoint_writes_the_unlink_through(tmp_path, monkeypatch):
    """End-to-end: the handler persists the unlink, NOT under the catalog lock."""
    import api.routes as routes

    pid = "proj_round8_endpoint"
    sid = "sess-round8-endpoint"
    projects = [
        {"project_id": pid, "name": "Gone", "profile": "default", "workspaces": []}
    ]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sidecar = _sidecar_for(session_dir, sid, pid)

    catalog = _ProbeLock()
    monkeypatch.setattr(
        routes, "load_projects", lambda *a, **k: [dict(p) for p in projects]
    )
    monkeypatch.setattr(
        routes,
        "save_projects",
        lambda ps: projects.__setitem__(slice(None), [dict(p) for p in ps]),
    )
    # No index: the index pass cannot be what rewrites the sidecar here.
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", tmp_path / "missing_index.json")
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    _, sessions, locks = _install_clear_stubs(
        monkeypatch, session_dir, catalog_probe=catalog
    )
    row = _CachedRow(sid, pid, sidecar, locks, catalog_probe=catalog)
    sessions[sid] = row

    responses = []
    assert (
        _post_project_route(monkeypatch, _DELETE_PATH, {"project_id": pid}, responses)
        is True
    )
    assert [r["status"] for r in responses] == [200], responses

    assert row.project_id is None
    assert row.save_calls == [(None, False, True, False)], row.save_calls
    assert json.loads(sidecar.read_text(encoding="utf-8"))["project_id"] is None
    assert [p["project_id"] for p in projects] == [], projects


# ---------------------------------------------------------------------------
# 4 — one catalog lock, shared with the catalog layer
# ---------------------------------------------------------------------------


def test_the_catalog_lock_is_the_catalog_layer_object():
    """``routes._PROJECTS_CATALOG_LOCK`` IS ``models.PROJECTS_CATALOG_LOCK``."""
    import api.models as models
    import api.routes as routes

    assert routes._PROJECTS_CATALOG_LOCK is models.PROJECTS_CATALOG_LOCK


def test_ensure_cron_project_holds_the_shared_catalog_lock(monkeypatch):
    """The cron system-project load→save pair is atomic against the routes."""
    import api.models as models

    probe = _ProbeLock()
    monkeypatch.setattr(models, "PROJECTS_CATALOG_LOCK", probe)
    monkeypatch.setattr(models, "load_projects", lambda *a, **k: [])
    seen = []

    def _save_projects(rows):
        seen.append((probe.held(), [p.get("name") for p in rows]))

    monkeypatch.setattr(models, "save_projects", _save_projects)

    project_id = models.ensure_cron_project(profile="default")
    assert project_id, "the cron project must be created"
    assert seen == [(True, [models.CRON_PROJECT_NAME])], seen


def test_ensure_webhook_project_holds_the_shared_catalog_lock(monkeypatch):
    """Same for the webhooks system project."""
    import api.models as models

    probe = _ProbeLock()
    monkeypatch.setattr(models, "PROJECTS_CATALOG_LOCK", probe)
    monkeypatch.setattr(models, "load_projects", lambda *a, **k: [])
    seen = []

    def _save_projects(rows):
        seen.append((probe.held(), [p.get("name") for p in rows]))

    monkeypatch.setattr(models, "save_projects", _save_projects)

    project_id = models.ensure_webhook_project(profile="default")
    assert project_id, "the webhooks project must be created"
    assert seen == [(True, [models.WEBHOOK_PROJECT_NAME])], seen


def test_every_catalog_pair_takes_the_shared_lock():
    """Source guard: the catalog lock lives in the catalog layer and is shared."""
    src = _read("api/routes.py")
    assert "from api.models import PROJECTS_CATALOG_LOCK as _PROJECTS_CATALOG_LOCK" in src
    assert "_PROJECTS_CATALOG_LOCK = threading.RLock()" not in src

    models_src = _read("api/models.py")
    assert "PROJECTS_CATALOG_LOCK = threading.RLock()" in models_src
    for fn in ("ensure_cron_project", "ensure_webhook_project"):
        start = models_src.index(f"def {fn}(")
        body = models_src[start : start + 2600]
        assert "PROJECTS_CATALOG_LOCK" in body, f"{fn} does not take the shared lock"
        assert body.index("PROJECTS_CATALOG_LOCK") < body.index("load_projects()"), (
            f"{fn} reads the catalog before taking the shared lock"
        )


@pytest.mark.parametrize("path", ["api/routes.py", "api/models.py"])
def test_changed_modules_compile(path):
    compile((REPO_ROOT / path).read_text(encoding="utf-8"), path, "exec")
