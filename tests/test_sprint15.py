"""
Sprint 15 Tests: session projects (CRUD, move, backward compat).
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, d["session"]


def make_project(created_list, name="Test Project", color=None):
    body = {"name": name}
    if color:
        body["color"] = color
    d, status = post("/api/projects/create", body)
    assert status == 200
    pid = d["project"]["project_id"]
    created_list.append(pid)
    return pid, d["project"]


def cleanup_projects(project_ids):
    for pid in project_ids:
        try:
            post("/api/projects/delete", {"project_id": pid})
        except Exception:
            pass


# ── Project CRUD ─────────────────────────────────────────────────────────


def test_create_project():
    """Creating a project returns a valid project dict."""
    pids = []
    try:
        pid, proj = make_project(pids, "My Project", "#7cb9ff")
        assert pid and len(pid) == 12
        assert proj["name"] == "My Project"
        assert proj["color"] == "#7cb9ff"
        assert "created_at" in proj
    finally:
        cleanup_projects(pids)


def test_list_projects_empty():
    """Listing projects when none exist returns empty list."""
    d, status = get("/api/projects")
    assert status == 200
    assert isinstance(d["projects"], list)


def test_list_projects():
    """Listing projects returns created projects."""
    pids = []
    try:
        make_project(pids, "Alpha")
        make_project(pids, "Beta")
        d, status = get("/api/projects")
        assert status == 200
        names = [p["name"] for p in d["projects"]]
        assert "Alpha" in names
        assert "Beta" in names
    finally:
        cleanup_projects(pids)


def test_rename_project():
    """Renaming a project updates its name."""
    pids = []
    try:
        pid, _ = make_project(pids, "Old Name")
        d, status = post(
            "/api/projects/rename", {"project_id": pid, "name": "New Name"}
        )
        assert status == 200
        assert d["project"]["name"] == "New Name"
        # Verify via list
        dl, _ = get("/api/projects")
        names = [p["name"] for p in dl["projects"]]
        assert "New Name" in names
        assert "Old Name" not in names
    finally:
        cleanup_projects(pids)


def test_delete_project():
    """Deleting a project removes it from the list."""
    pids = []
    try:
        pid, _ = make_project(pids, "Doomed")
        d, status = post("/api/projects/delete", {"project_id": pid})
        assert status == 200
        assert d["ok"] is True
        dl, _ = get("/api/projects")
        assert all(p["project_id"] != pid for p in dl["projects"])
        pids.clear()  # already deleted
    finally:
        cleanup_projects(pids)


def test_delete_project_unassigns_sessions():
    """Deleting a project unassigns all sessions that belonged to it."""
    pids = []
    sids = []
    try:
        pid, _ = make_project(pids, "Temp Project")
        sid, _ = make_session(sids)
        # Assign session to project
        post("/api/session/move", {"session_id": sid, "project_id": pid})
        # Verify assigned
        sd, _ = get(f"/api/session?session_id={sid}")
        assert sd["session"].get("project_id") == pid
        # Delete project
        post("/api/projects/delete", {"project_id": pid})
        pids.clear()
        # Verify session is unassigned
        sd2, _ = get(f"/api/session?session_id={sid}")
        assert sd2["session"].get("project_id") is None
    finally:
        cleanup_projects(pids)
        for s in sids:
            post("/api/session/delete", {"session_id": s})


def test_create_project_requires_name():
    """Creating a project without a name returns 400."""
    d, status = post("/api/projects/create", {})
    assert status == 400


def test_delete_nonexistent_project():
    """Deleting a project that doesn't exist returns 404."""
    d, status = post("/api/projects/delete", {"project_id": "nonexistent99"})
    assert status == 404


# ── Session move ─────────────────────────────────────────────────────────


def test_session_move_to_project():
    """Moving a session to a project sets its project_id."""
    pids = []
    sids = []
    try:
        pid, _ = make_project(pids, "Work")
        sid, _ = make_session(sids)
        d, status = post("/api/session/move", {"session_id": sid, "project_id": pid})
        assert status == 200
        assert d["session"]["project_id"] == pid
    finally:
        cleanup_projects(pids)
        for s in sids:
            post("/api/session/delete", {"session_id": s})


def test_session_move_to_unassigned():
    """Moving a session to null project unassigns it."""
    pids = []
    sids = []
    try:
        pid, _ = make_project(pids, "Temp")
        sid, _ = make_session(sids)
        # Assign then unassign
        post("/api/session/move", {"session_id": sid, "project_id": pid})
        d, status = post("/api/session/move", {"session_id": sid, "project_id": None})
        assert status == 200
        assert d["session"]["project_id"] is None
    finally:
        cleanup_projects(pids)
        for s in sids:
            post("/api/session/delete", {"session_id": s})


def test_session_project_in_list():
    """Session list includes project_id for assigned sessions."""
    pids = []
    sids = []
    try:
        pid, _ = make_project(pids, "Listed")
        sid, _ = make_session(sids)
        # Give it a title so it shows in list (non-empty Untitled sessions are hidden)
        post(
            "/api/session/rename", {"session_id": sid, "title": "Project Test Session"}
        )
        post("/api/session/move", {"session_id": sid, "project_id": pid})
        dl, _ = get("/api/sessions")
        match = [s for s in dl["sessions"] if s["session_id"] == sid]
        assert len(match) == 1
        assert match[0]["project_id"] == pid
    finally:
        cleanup_projects(pids)
        for s in sids:
            post("/api/session/delete", {"session_id": s})


# ── Backward compat ──────────────────────────────────────────────────────


def test_compact_includes_project_id():
    """New session compact dict includes project_id as null."""
    sids = []
    try:
        sid, sess = make_session(sids)
        # Give it a title so it appears in the list
        post("/api/session/rename", {"session_id": sid, "title": "Compat Test"})
        dl, _ = get("/api/sessions")
        match = [s for s in dl["sessions"] if s["session_id"] == sid]
        assert len(match) == 1
        assert "project_id" in match[0]
        assert match[0]["project_id"] is None
    finally:
        for s in sids:
            post("/api/session/delete", {"session_id": s})


def test_session_move_requires_session_id():
    """Moving without session_id returns 400."""
    d, status = post("/api/session/move", {"project_id": "abc"})
    assert status == 400
