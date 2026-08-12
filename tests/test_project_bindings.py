"""Tests for project bindings (/api/projects/bind).

A project can pin a workspace / model / reasoning effort so the quick-create
(+) button opens a new session pre-configured with that project's context.

- Bind fields persist to projects.json.
- workspace binding auto-registers the path in the saved workspace list.
- Invalid effort / nonexistent workspace are rejected.
- null/'' unbinds a field.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

from tests._pytest_port import BASE


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


HOME_WS = str(Path.home())


def _create_project():
    res = _post("/api/projects/create", {"name": "bind-test", "color": "#50c878"})
    assert res.get("ok"), f"create failed: {res}"
    return res["project"]["project_id"]


def test_bind_workspace_model_effort_roundtrip(tmp_path):
    pid = _create_project()
    ws_dir = tmp_path / "roundtrip-ws"
    ws_dir.mkdir()
    ws_str = str(ws_dir)
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": ws_str,
        "model": "test-model-1",
        "model_provider": "custom:test",
        "reasoning_effort": "high",
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert p["workspace"] == str(ws_dir.resolve())
    assert p["model"] == "test-model-1"
    assert p["model_provider"] == "custom:test"
    assert p["reasoning_effort"] == "high"

    # Persisted on disk via GET /api/projects
    proj_res = _get("/api/projects")
    projects = proj_res.get("projects", [])
    saved = next((x for x in projects if x["project_id"] == pid), None)
    assert saved is not None, "bound project must appear in /api/projects"
    assert saved.get("reasoning_effort") == "high"


def test_bind_workspace_auto_registers_in_workspace_list(tmp_path):
    """A workspace outside the default saved list is auto-registered so an
    admin-style path (e.g. D:\\projects\\...) can be bound in one step."""
    pid = _create_project()
    import pathlib
    ws_dir = tmp_path / "bound-ws"
    ws_dir.mkdir()
    # Use the native path string (no manual slash translation — Windows and
    # POSIX CI both pass through; the server canonicalizes to the platform
    # form before storing).
    ws_str = str(ws_dir)
    res = _post("/api/projects/bind", {"project_id": pid, "workspace": ws_str})
    assert res.get("ok"), f"bind failed: {res}"
    assert pathlib.Path(res["project"]["workspace"]).resolve() == ws_dir.resolve()

    # The path must now be in the saved workspace list.
    ws_res = _get("/api/workspaces")
    assert any(
        pathlib.Path(w["path"]).resolve() == ws_dir.resolve()
        for w in ws_res.get("workspaces", [])
    ), "bound workspace must be auto-registered in the workspace list"


def test_bind_rejects_invalid_effort():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "reasoning_effort": "insane",
    })
    assert "error" in res, "invalid effort must be rejected"
    assert "reasoning_effort" in res["error"]


def test_bind_rejects_nonexistent_workspace():
    pid = _create_project()
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": "Z:/definitely/not/here",
    })
    assert "error" in res, "nonexistent workspace must be rejected"


def test_bind_null_unbinds_field():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": HOME_WS,
        "reasoning_effort": "medium",
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspace": None,
        "reasoning_effort": None,
    })
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspace" not in p
    assert "reasoning_effort" not in p


def test_bind_unknown_project_404():
    res = _post("/api/projects/bind", {
        "project_id": "deadbeef0000",
        "workspace": HOME_WS,
    })
    assert "error" in res, "unknown project must be rejected"


def test_bind_multiple_workspaces_with_default(tmp_path):
    """A project can bind several workspaces; the marked default drives new
    sessions, and default must always be a member of the workspaces list."""
    pid = _create_project()
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a), str(ws_b)

    res = _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [a, b],
        "default_workspace": b,
        "auto_assign": True,
    })
    assert res.get("ok"), f"bind failed: {res}"
    p = res["project"]
    assert set(p["workspaces"]) == {a, b}, p["workspaces"]
    assert p["default_workspace"] == b
    assert p.get("auto_assign") is True

    # Default can also auto-add itself to the list (invariant holds).
    res2 = _post("/api/projects/bind", {
        "project_id": pid,
        "default_workspace": a,
    })
    p2 = res2["project"]
    assert p2["default_workspace"] == a
    assert a in p2["workspaces"]


def test_bind_workspaces_clear_with_null():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [HOME_WS],
        "default_workspace": HOME_WS,
    })
    res = _post("/api/projects/bind", {"project_id": pid, "workspaces": None})
    assert res.get("ok"), f"unbind failed: {res}"
    p = res["project"]
    assert "workspaces" not in p
    assert "default_workspace" not in p


def test_bind_auto_assign_off():
    pid = _create_project()
    _post("/api/projects/bind", {
        "project_id": pid,
        "workspaces": [HOME_WS],
        "auto_assign": True,
    })
    res = _post("/api/projects/bind", {
        "project_id": pid,
        "auto_assign": False,
    })
    assert res.get("ok")
    assert "auto_assign" not in res["project"]


def test_bind_workspaces_replace_keeps_legacy_alias_in_sync(tmp_path):
    """Replacing the workspace list must keep the legacy single `workspace`
    alias consistent (first entry, or removed when the list empties)."""
    pid = _create_project()
    ws_a = tmp_path / "alias-a"
    ws_b = tmp_path / "alias-b"
    ws_a.mkdir()
    ws_b.mkdir()
    a, b = str(ws_a), str(ws_b)

    r1 = _post("/api/projects/bind", {"project_id": pid, "workspaces": [a, b]})
    assert r1.get("ok")
    assert r1["project"]["workspace"] == a, "alias must mirror first entry"

    r2 = _post("/api/projects/bind", {"project_id": pid, "workspaces": [b]})
    assert r2.get("ok")
    assert r2["project"]["workspace"] == b, "alias must follow replacement"

    r3 = _post("/api/projects/bind", {"project_id": pid, "workspaces": None})
    assert r3.get("ok")
    assert "workspace" not in r3["project"], "alias must vanish when cleared"


def test_auto_assign_project_for_workspace(tmp_path, monkeypatch):
    """_auto_assign_project_for_workspace picks the owning project in list order."""
    import api.routes as routes

    pid1 = _create_project()
    pid2 = _create_project()
    ws = tmp_path / "shared"
    ws.mkdir()
    ws_str = str(ws)
    _post("/api/projects/bind", {
        "project_id": pid1,
        "workspaces": [ws_str],
        "auto_assign": True,
    })
    _post("/api/projects/bind", {
        "project_id": pid2,
        "workspaces": [ws_str],
        "auto_assign": True,
    })

    # First match in on-disk list order wins; reload from disk to avoid cache.
    assert routes._auto_assign_project_for_workspace(ws_str) in (pid1, pid2)

    # Unclaimed workspace → None
    assert routes._auto_assign_project_for_workspace("Z:/not/claimed") is None
    # Disabled flag → None
    _post("/api/projects/bind", {"project_id": pid1, "auto_assign": False})
    _post("/api/projects/bind", {"project_id": pid2, "auto_assign": False})
    assert routes._auto_assign_project_for_workspace(ws_str) is None


def test_auto_assign_project_omitted_profile_resolves_to_active(monkeypatch):
    """Greptile P1 (#6836): an omitted ``profile`` must resolve to the ACTIVE
    profile (same rule as ``new_session``), never to None-as-default.

    Otherwise a named-profile caller that omits ``profile`` could have its
    session auto-assigned to a default-profile project (or vice versa) —
    the auto-assign matcher and the session creator disagree on the
    effective profile.
    """
    import api.routes as routes

    ws = "C:/Users/Admin/workspace"
    default_proj = {
        "project_id": "proj_default", "name": "d",
        "profile": "default", "workspaces": [ws], "auto_assign": True,
    }
    named_proj = {
        "project_id": "proj_work", "name": "w",
        "profile": "work", "workspaces": [ws], "auto_assign": True,
    }
    monkeypatch.setattr(routes, "load_projects", lambda: [default_proj, named_proj])

    # Active profile = default: omitted profile resolves to default → the
    # default project must win, NOT the named one.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert routes._auto_assign_project_for_workspace(ws) == "proj_default"
    # Explicit named profile → the named project wins.
    assert routes._auto_assign_project_for_workspace(ws, profile="work") == "proj_work"

    # Active profile = named (e.g. caller is operating inside profile "work"):
    # an omitted profile must resolve to "work", so the named project wins and
    # the default project is NOT matched (cross-profile pollution).
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    assert routes._auto_assign_project_for_workspace(ws) == "proj_work"

    # The pre-fix bug: with profile=None (unpatched helper), the default
    # project used to be returned even when the ACTIVE profile is a named one
    # (condition `p.get("profile") and profile` short-circuited). Regression
    # guard: an explicit None must behave identically to the active profile.
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    assert routes._auto_assign_project_for_workspace(ws, profile=None) == "proj_work"


def test_apply_project_auto_assign_files_existing_sessions(tmp_path, monkeypatch):
    """_apply_project_auto_assign re-files existing sessions whose workspace
    is bound, skipping cross-profile rows and already-owned sessions."""
    import api.routes as routes

    # Point the index at a temp file with three sessions: two in the bound
    # workspace, one in another workspace.
    ws = tmp_path / "bound-ws"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_aaa", "workspace": ws_str, "profile": "default",
         "project_id": None, "message_count": 3},
        {"session_id": "sess_bbb", "workspace": ws_str, "profile": "default",
         "project_id": "already-owned", "message_count": 1},
        {"session_id": "sess_ccc", "workspace": HOME_WS,
         "profile": "default", "project_id": None, "message_count": 2},
        {"session_id": "sess_ddd", "workspace": ws_str, "profile": "other",
         "project_id": None, "message_count": 4},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)

    saved = {}
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
        def save(self):
            saved[self.session_id] = self.project_id

    monkeypatch.setattr(routes, "get_session",
                        lambda sid: _FakeSession(sid) if sid in (
                            "sess_aaa", "sess_bbb", "sess_ccc", "sess_ddd") else None)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    proj = {"project_id": "proj_xyz", "profile": "default",
            "workspaces": [ws_str], "auto_assign": True}
    changed = routes._apply_project_auto_assign(proj)

    # sess_aaa: bound ws, unowned → re-filed.
    assert saved.get("sess_aaa") == "proj_xyz"
    # sess_bbb: already owned by another project → untouched.
    assert "sess_bbb" not in saved
    # sess_ccc: different workspace → untouched.
    assert "sess_ccc" not in saved
    # sess_ddd: other profile → untouched.
    assert "sess_ddd" not in saved
    assert changed == 1


def test_apply_project_auto_assign_named_profile_never_sweeps_default(tmp_path, monkeypatch):
    """A NAMED-profile project must not re-file default/unprofiled sessions —
    they would end up tagged with a foreign project_id."""
    import api.routes as routes

    ws = tmp_path / "named-ws"
    ws.mkdir()
    ws_str = str(ws)
    index_file = tmp_path / "_index.json"
    index_file.write_text(json.dumps([
        {"session_id": "sess_def", "workspace": ws_str, "profile": "default",
         "project_id": None, "message_count": 1},
        {"session_id": "sess_none", "workspace": ws_str, "profile": None,
         "project_id": None, "message_count": 1},
        {"session_id": "sess_haku", "workspace": ws_str, "profile": "haku",
         "project_id": None, "message_count": 1},
    ]))
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)

    saved = {}
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.project_id = None
        def save(self):
            saved[self.session_id] = self.project_id

    monkeypatch.setattr(routes, "get_session",
                        lambda sid: _FakeSession(sid) if sid in (
                            "sess_def", "sess_none", "sess_haku") else None)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    proj = {"project_id": "proj_named", "profile": "haku",
            "workspaces": [ws_str], "auto_assign": True}
    changed = routes._apply_project_auto_assign(proj)

    assert saved.get("sess_haku") == "proj_named"
    assert "sess_def" not in saved, "named project must not sweep default sessions"
    assert "sess_none" not in saved, "named project must not sweep unprofiled sessions"
    assert changed == 1
