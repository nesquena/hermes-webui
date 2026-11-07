"""Round-8 item 2 (re-gate 2026-10-12, head c67105b8 / 4e3f8052): GET
/api/workspaces regression on SSH/Docker profiles.

Inner profile-level probes (cwd + blocked-roots) are already call-scoped.
The leftover is the *outer* per-row classification: merge re-runs
``row_db_key`` after ``_stamp_remote_kind`` already decided remoteness, and
GET /api/workspaces still classifies once in ``load_workspaces`` and again
in merge under a nested empty memo.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import api.projects_bridge as bridge
import api.workspace as workspace


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path)
        );
        """
    )
    for i, p in enumerate(projects):
        conn.execute(
            "INSERT INTO projects (id, slug, name, created_at, archived) VALUES (?,?,?,?,?)",
            (p["id"], p["slug"], p["name"], 1000 + i, p.get("archived", 0)),
        )
        for j, folder in enumerate(p.get("folders", [])):
            conn.execute(
                "INSERT INTO project_folders (project_id, path, is_primary, added_at) VALUES (?,?,?,?)",
                (p["id"], folder, 1 if j == 0 else 0, 2000 + j),
            )
    conn.commit()
    conn.close()
    return db


def _count_candidate_calls(monkeypatch):
    orig = workspace._remote_terminal_workspace_candidate_uncached
    calls: list[str] = []

    def _counted(path, profile=None):
        calls.append(str(path))
        return orig(path, profile=profile)

    monkeypatch.setattr(workspace, "_remote_terminal_workspace_candidate_uncached", _counted)
    return calls


def test_merge_reuses_remote_cwd_and_blocked_roots_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    # No shared store: the merge takes the pass-through projection, which
    # still stamps remote kind per row (round 7 blocker 3).
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: None)

    cwd_calls: list[int] = []
    roots_calls: list[int] = []

    def _counted_cwd(profile=None):
        cwd_calls.append(1)
        return "/remote/work"

    orig_roots = workspace._workspace_blocked_roots

    def _counted_roots():
        roots_calls.append(1)
        return orig_roots()

    monkeypatch.setattr(workspace, "_remote_terminal_cwd", _counted_cwd)
    monkeypatch.setattr(workspace, "_workspace_blocked_roots", _counted_roots)

    N = 8
    rows = [{"path": f"/remote/work/p{i}", "name": f"p{i}"} for i in range(N)]
    out = bridge.merge_hermes_projects(rows, profile_home=None, profile="default")

    # Behavior must be unchanged: every remote row keeps its explicit kind.
    assert len(out) == N
    assert all(w.get("workspace_kind") == "remote" for w in out)

    # Profile-level work happens ONCE per merge call, not once per row.
    assert len(cwd_calls) == 1, (
        f"remote terminal cwd re-read {len(cwd_calls)} times for one merge call "
        f"(config read per row — the SSH/Docker latency finding)"
    )
    # Blocked-root builders are also profile/process constants: a handful of
    # first-call builds is fine, a per-row rebuild is the regression.
    assert len(roots_calls) <= 4, (
        f"blocked-roots table rebuilt {len(roots_calls)} times in one merge call "
        f"(each rebuild is ~45 Path.resolve() calls)"
    )


def test_scope_memo_is_call_scoped_not_process_lifetime(monkeypatch):
    """The memo must not leak across merge calls: a config change between two
    requests is visible on the next one (PR #7636 lesson)."""
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: None)

    values = iter(["/remote/a", "/remote/b"])
    monkeypatch.setattr(workspace, "_remote_terminal_cwd",
                        lambda profile=None: next(values))

    rows_a = [{"path": "/remote/a/x", "name": "x"}]
    out_a = bridge.merge_hermes_projects(rows_a, profile_home=None, profile="default")
    assert out_a[0].get("workspace_kind") == "remote"

    rows_b = [{"path": "/remote/b/y", "name": "y"}]
    out_b = bridge.merge_hermes_projects(rows_b, profile_home=None, profile="default")
    assert out_b[0].get("workspace_kind") == "remote", (
        "second merge call must see the NEW cwd — a process-lifetime memo "
        "would still answer /remote/a and drop the kind stamp"
    )


def test_merge_classifies_each_row_once_when_db_present(tmp_path, monkeypatch):
    """Readable store takes the inner loop; stamp already classified the row.

    ``row_db_key`` must not re-enter ``_remote_terminal_workspace_candidate``
    for the same path (re-gate 4e3f8052 leftover).
    """
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    home = tmp_path / "home"
    home.mkdir()
    host = tmp_path / "hostproj"
    host.mkdir()
    _make_projects_db(home, [
        {"id": "p1", "slug": "host", "name": "Host", "folders": [str(host)]},
    ])
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: home / "projects.db")
    bridge._cache.clear()
    monkeypatch.setattr(workspace, "_remote_terminal_cwd",
                        lambda profile=None: "/remote/work")
    calls = _count_candidate_calls(monkeypatch)

    n = 8
    rows = [{"path": f"/remote/work/p{i}", "name": f"p{i}"} for i in range(n)]
    out = bridge.merge_hermes_projects(rows, profile_home=home, profile="default")
    assert len(out) == n + 1
    assert all(
        w.get("workspace_kind") == "remote"
        for w in out if str(w.get("path", "")).startswith("/remote/work/")
    )
    remote_calls = [c for c in calls if c.startswith("/remote/work/")]
    assert len(remote_calls) == n, (
        f"classified remote rows {len(remote_calls)} times for {n} rows "
        f"(stamp + row_db_key re-classify)"
    )


def test_nested_merge_reuses_parent_call_scope_memo(tmp_path, monkeypatch):
    """GET wraps load+merge in one scope; merge must not wipe the parent memo."""
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: None)
    monkeypatch.setattr(workspace, "_remote_terminal_cwd",
                        lambda profile=None: "/remote/work")
    calls = _count_candidate_calls(monkeypatch)

    n = 6
    rows = [{"path": f"/remote/work/p{i}", "name": f"p{i}"} for i in range(n)]
    with workspace.workspace_call_scope():
        for row in rows:
            workspace.cached_remote_workspace_candidate(row["path"], profile="default")
        primed = len(calls)
        out = bridge.merge_hermes_projects(rows, profile_home=None, profile="default")
    assert len(out) == n
    assert all(w.get("workspace_kind") == "remote" for w in out)
    assert len(calls) == primed, (
        f"nested merge re-classified {len(calls) - primed} already-primed rows "
        f"(empty nested workspace_call_scope dropped the parent memo)"
    )


def test_get_workspaces_classifies_each_saved_row_once(tmp_path, monkeypatch):
    """GET /api/workspaces: one classification per saved row across load+merge."""
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    home = tmp_path / "home"
    home.mkdir()
    host = tmp_path / "hostproj"
    host.mkdir()
    _make_projects_db(home, [
        {"id": "p1", "slug": "host", "name": "Host", "folders": [str(host)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr(bridge, "_projects_db_path", lambda profile_home: home / "projects.db")
    bridge._cache.clear()
    monkeypatch.setattr(workspace, "_remote_terminal_cwd",
                        lambda profile=None: "/remote/work")
    calls = _count_candidate_calls(monkeypatch)

    n = 8
    rows = [{"path": f"/remote/work/p{i}", "name": f"p{i}"} for i in range(n)]
    ws_file = workspace._workspaces_file(profile="default")
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps(rows), encoding="utf-8")
    lw_file = workspace._last_workspace_file(profile="default")
    lw_file.write_text(rows[0]["path"], encoding="utf-8")

    import api.routes as routes
    from api.routes import handle_get

    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)

    handler = MagicMock()
    handler.wfile = MagicMock()
    parsed = urlparse("http://example.com/api/workspaces")
    handle_get(handler, parsed)
    assert handler.send_response.call_args[0][0] == 200

    remote_calls = [c for c in calls if c.startswith("/remote/work/")]
    assert len(set(remote_calls)) == n
    assert len(remote_calls) == n, (
        f"GET classified {len(remote_calls)} times for {n} saved rows "
        f"(load + stamp + row_db_key, nested empty memo)"
    )
