"""Regression tests for the deep-audit findings on the projects.db bridge.

Each test pins a fix from the full-branch audit:

- P1  symlink normalization: local rows are stored symlink-resolved while
  projects.db may hold a symlinked spelling; every bridge comparison must
  canonicalize so a shared-backed workspace is never misclassified as
  local-only (silent revert on next poll).
- P2  load_project_state contract: archived mapping is a dict on EVERY path
  (kill switch, no DB, failed read), never a bare set.
- P2  reorder dedupe: two spellings of one directory in one request must not
  persist two identical rows.
- P3  archived mirror matching: empty names never match; multiple archived
  projects at one path all hide their mirrors.
- P3  rename accepts the same path spellings remove accepts.
- Maintainer blocker guard: the "Register as Hermes Project" checkbox stays
  unchecked by default in the form source.
"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.projects_bridge import (
    _cache,
    archive_hermes_project,
    load_project_state,
    merge_hermes_projects,
    path_key,
    rename_hermes_project,
)



def _manager_available() -> bool:
    """A Hermes Projects manager is reachable (in-process import or agent
    checkout for the subprocess fallback). CI's tests.yml installs pip
    packages only — no agent checkout — so write-path tests must skip there
    (re-gate should-fix 5)."""
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


requires_manager = pytest.mark.usefixtures("_requires_manager")


@pytest.fixture
def _requires_manager():
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")


@pytest.fixture(autouse=True)
def _preimport_lazy_state_wal():
    """Upstream ``open_db`` imports ``hermes_state_wal`` lazily at connect()
    time; conftest's per-test sys.path restore can drop the agent dir
    between tests, so the lazy import fails mid-suite (passes alone, fails
    in a multi-file run). Pre-import it here — adding the agent dir to
    sys.path ourselves when a prior test stripped it — so the module cache
    answers the lazy import (re-gate should-fix 5). conftest restores
    sys.path after the test, so the append does not leak."""
    import sys
    try:
        import api.config  # noqa: F401  (normally appends the agent dir)
        from api.projects_bridge import _agent_dir
        d = _agent_dir()
        # A prior test's sys.path restore can strip the agent dir entirely —
        # re-add it for THIS test (conftest restores sys.path afterwards, so
        # the append never leaks) so hermes_cli AND its lazy hermes_state_wal
        # import both keep resolving mid-suite.
        if d is not None and str(d) not in sys.path:
            sys.path.append(str(d))
        import hermes_state_wal  # noqa: F401
    except Exception:
        pass
    yield

@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    _cache.clear()
    yield
    _cache.clear()


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


def _make_handler():
    h = MagicMock()
    h.wfile = MagicMock()
    return h


def _response(handler):
    body = b"".join(c.args[0] for c in handler.wfile.write.call_args_list)
    return json.loads(body)


# ── P1: symlink normalization ───────────────────────────────────────────────


def test_path_key_folds_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert path_key(str(link)) == path_key(str(real))
    # trailing separator and whitespace variants
    assert path_key(f"  {real}/ ") == path_key(str(real))


def test_merge_no_double_listing_for_symlinked_db_path(tmp_path):
    """DB row under a symlinked spelling + local row under the real path must
    merge into ONE entry, not list the same directory twice."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj", "folders": [str(link)]},
    ])
    merged = merge_hermes_projects([{"path": str(real), "name": "Proj"}], profile_home=tmp_path)
    assert len(merged) == 1
    assert merged[0]["source"] == "hermes_project"
    assert merged[0]["name"] == "Proj"


@requires_manager
def test_archive_finds_project_under_symlinked_db_path(tmp_path):
    """archive_hermes_project(resolved real path) must find the DB row stored
    under a symlinked spelling — otherwise the route's shared-backed remove
    silently no-ops and the next GET re-appends the project."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj", "folders": [str(link)]},
    ])
    result = archive_hermes_project(str(real), profile_home=tmp_path)
    assert result.get("archived") is True
    assert load_project_state(profile_home=tmp_path)[0] == []


@requires_manager
def test_remove_shared_backed_via_symlink_no_silent_revert(tmp_path, monkeypatch):
    """Route-level: local row under the real path, DB row under a symlinked
    path. Remove must archive the shared project (not take the local-only
    branch) so the next GET does not re-append the retired project."""
    from api.routes import _handle_workspace_remove

    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj", "folders": [str(link)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    local_row = {"path": str(real), "name": "Proj"}
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[dict(local_row)]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", list(wss))):
        _handle_workspace_remove(handler, {"path": str(real)})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    assert [w["path"] for w in resp["workspaces"]] == []
    # The DB row really got archived: a fresh read shows nothing to re-append.
    from api.projects_bridge import load_hermes_project_workspaces
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []


# ── P2: archived mapping contract ───────────────────────────────────────────


def test_kill_switch_archived_is_dict(tmp_path, monkeypatch):
    """Contract: archived_by_path is a dict on every path — a caller may
    .get() it. The kill-switch shortcut used to return a bare set."""
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    entries, archived, ok = load_project_state(profile_home=tmp_path)
    assert ok and entries == [] and isinstance(archived, dict)


def test_missing_db_archived_is_dict(tmp_path):
    entries, archived, ok = load_project_state(profile_home=tmp_path / "nonexistent")
    assert ok and entries == [] and isinstance(archived, dict)


# ── P3: archived mirror edge cases ──────────────────────────────────────────


def test_archived_empty_name_does_not_hide_blank_local_row(tmp_path):
    """An archived project with no name must not hide a blank-named local
    workspace at that path (empty == empty used to match)."""
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "", "folders": ["/srv/a"], "archived": 1},
    ])
    local = [{"path": "/srv/a", "name": ""}]
    assert merge_hermes_projects(local, profile_home=tmp_path) == local


def test_multiple_archived_names_at_one_path_all_hidden(tmp_path):
    """Archive A at /p, re-create as B at /p, archive B: mirrors carrying
    EITHER name are retired mirrors and both must stay hidden."""
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "X", "folders": ["/srv/p"], "archived": 1},
        {"id": "p2", "slug": "b", "name": "Y", "folders": ["/srv/p"], "archived": 1},
    ])
    merged = merge_hermes_projects(
        [{"path": "/srv/p", "name": "Y", "project_mirror": True}], profile_home=tmp_path)
    assert merged == []


# ── P2: reorder dedupe ───────────────────────────────────────────────────────


def test_reorder_duplicate_spellings_persist_one_row(tmp_path, monkeypatch):
    from api.routes import _handle_workspace_reorder

    p = tmp_path / "srv" / "proj"
    p.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj", "folders": [str(p)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", list(wss))):
        _handle_workspace_reorder(handler, {"paths": [str(p), str(p) + "/"]})
    saved_rows = [w for w in saved["wss"] if w["path"] == str(p)]
    assert len(saved_rows) == 1, f"duplicate rows persisted: {saved['wss']}"


# ── P3: rename accepts remove-compatible spellings ─────────────────────────


def test_rename_local_only_trailing_slash(tmp_path, monkeypatch):
    """Remove normalizes trailing slashes; rename must too — a local-only
    workspace renamed via '/path/' must not 404."""
    from api.routes import _handle_workspace_rename

    _make_projects_db(tmp_path, [])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    local = {"path": "/home/user/proj", "name": "Old"}
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[dict(local)]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", list(wss))):
        _handle_workspace_rename(handler, {"path": "/home/user/proj/", "name": "New"})
    handler.send_response.assert_called_once_with(200)
    assert saved["wss"][0]["name"] == "New"


# ── Maintainer blocker guard: checkbox unchecked by default ─────────────────


def test_register_as_project_checkbox_unchecked_by_default():
    src = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    assert "workspaceFormAsProject" in src
    # The checkbox markup must not carry a checked attribute.
    for line in src.splitlines():
        if 'id="workspaceFormAsProject"' in line:
            assert "checked" not in line, f"checkbox must default to unchecked: {line.strip()}"


# ── greptile P1: distinct projects sharing one canonical path ───────────────


@requires_manager
def test_archive_refuses_ambiguous_path(tmp_path):
    """Two DB projects whose primary paths canonicalize to the same directory
    (real path + symlink spelling) collapse to one picker entry. Archive must
    refuse rather than pick one — the user targeted 'the folder', not one of
    two distinct shared projects behind it."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj A", "folders": [str(real)]},
        {"id": "p2", "slug": "b", "name": "Proj B", "folders": [str(link)]},
    ])
    result = archive_hermes_project(str(real), profile_home=tmp_path)
    assert result.get("archived") is False
    assert result.get("reason") == "ambiguous-path"
    assert result.get("count") == 2
    # Neither project was touched: both still active.
    entries, _arch, ok = load_project_state(profile_home=tmp_path)
    assert ok and len(entries) == 2


@requires_manager
def test_rename_refuses_ambiguous_path(tmp_path):
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj A", "folders": [str(real)]},
        {"id": "p2", "slug": "b", "name": "Proj B", "folders": [str(link)]},
    ])
    result = rename_hermes_project(str(real), "Renamed", profile_home=tmp_path)
    assert result.get("renamed") is False
    assert result.get("reason") == "ambiguous-path"
    entries, _arch, ok = load_project_state(profile_home=tmp_path)
    assert ok and {e["name"] for e in entries} == {"Proj A", "Proj B"}


@requires_manager
def test_archive_single_match_still_works_after_ambiguity_check(tmp_path):
    """Guard against the ambiguity check over-refusing: one project, one
    spelling — archive still succeeds."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj", "folders": [str(real)]},
    ])
    result = archive_hermes_project(str(real), profile_home=tmp_path)
    assert result.get("archived") is True


def test_remove_ambiguous_path_fails_closed(tmp_path, monkeypatch):
    """Route-level: an ambiguous shared path must surface an error, not
    silently archive one of the two projects."""
    from api.routes import _handle_workspace_remove

    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Proj A", "folders": [str(real)]},
        {"id": "p2", "slug": "b", "name": "Proj B", "folders": [str(link)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    saved = []
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[{"path": str(real), "name": "Proj A"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.append(list(wss))):
        _handle_workspace_remove(handler, {"path": str(real)})
    assert not saved, "local workspaces must be untouched when the shared owner is ambiguous"
    code = handler.send_response.call_args[0][0]
    assert code != 200, f"remove must fail closed on ambiguity, got {code}"
    # Both DB projects still active.
    entries, _arch, ok = load_project_state(profile_home=tmp_path)
    assert ok and len(entries) == 2


def test_merge_dedupes_two_db_rows_for_one_directory(tmp_path):
    """Low-severity re-gate item: two ACTIVE DB rows for one directory (e.g.
    registered through different symlink spellings) must list ONCE in the
    picker. Remove/rename already refuse the ambiguous case; the read side
    must not double-render it either."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": [str(real)]},
        {"id": "p2", "slug": "b", "name": "B", "folders": [str(link)]},
    ])
    _cache.clear()
    merged = merge_hermes_projects([], profile_home=tmp_path)
    assert len(merged) == 1


def test_merge_name_precedence_stable_with_local_row(tmp_path):
    """Greptile P2 (re-gate round): two active projects naming one directory
    must display the SAME name whether or not a local row exists. The append
    loop dedupes to the first DB row, so the name override must too — before
    the setdefault fix a reorder (which materializes a local row) flipped the
    displayed name from the first project's to the last project's."""
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "First", "folders": [str(real)]},
        {"id": "p2", "slug": "b", "name": "Second", "folders": [str(link)]},
    ])
    _cache.clear()
    without_row = merge_hermes_projects([], profile_home=tmp_path)
    _cache.clear()
    with_row = merge_hermes_projects(
        [{"path": str(real), "name": "Local Stale Name"}], profile_home=tmp_path
    )
    assert len(without_row) == 1 and len(with_row) == 1
    # Same displayed name in both views, and it is the FIRST project's.
    assert without_row[0]["name"] == "First"
    assert with_row[0]["name"] == "First"
