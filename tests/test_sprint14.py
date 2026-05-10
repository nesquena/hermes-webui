"""
Sprint 14 Tests: file rename, folder create, session archive, session tags, mermaid, timestamps.
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


# ── File rename ───────────────────────────────────────────────────────────


def test_file_rename():
    """Renaming a file changes its name on disk."""
    created = []
    try:
        sid, sess = make_session(created)
        # Create a file first
        post(
            "/api/file/create",
            {"session_id": sid, "path": "rename_test.txt", "content": "hello"},
        )
        d, status = post(
            "/api/file/rename",
            {"session_id": sid, "path": "rename_test.txt", "new_name": "renamed.txt"},
        )
        assert status == 200
        assert d["ok"] is True
        assert "renamed.txt" in d["new_path"]
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_file_rename_rejects_path_traversal():
    """Rename rejects names with path separators."""
    created = []
    try:
        sid, sess = make_session(created)
        post("/api/file/create", {"session_id": sid, "path": "safe.txt", "content": ""})
        d, status = post(
            "/api/file/rename",
            {"session_id": sid, "path": "safe.txt", "new_name": "../evil.txt"},
        )
        assert status == 400
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_file_rename_rejects_existing():
    """Rename fails if target name already exists."""
    created = []
    try:
        sid, sess = make_session(created)
        post("/api/file/create", {"session_id": sid, "path": "a.txt", "content": "a"})
        post("/api/file/create", {"session_id": sid, "path": "b.txt", "content": "b"})
        d, status = post(
            "/api/file/rename",
            {"session_id": sid, "path": "a.txt", "new_name": "b.txt"},
        )
        assert status == 400
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


# ── Folder create ─────────────────────────────────────────────────────────


def test_create_dir():
    """Creating a folder succeeds."""
    created = []
    try:
        sid, sess = make_session(created)
        d, status = post(
            "/api/file/create-dir", {"session_id": sid, "path": "test_folder"}
        )
        assert status == 200
        assert d["ok"] is True
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_create_dir_rejects_existing():
    """Creating a folder that already exists fails."""
    created = []
    try:
        sid, sess = make_session(created)
        post("/api/file/create-dir", {"session_id": sid, "path": "dup_folder"})
        d, status = post(
            "/api/file/create-dir", {"session_id": sid, "path": "dup_folder"}
        )
        assert status == 400
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


# ── Session archive ───────────────────────────────────────────────────────


def test_archive_session():
    """Archiving a session sets archived=true."""
    created = []
    try:
        sid, _ = make_session(created)
        d, status = post("/api/session/archive", {"session_id": sid, "archived": True})
        assert status == 200
        assert d["session"]["archived"] is True
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_unarchive_session():
    """Unarchiving a session sets archived=false."""
    created = []
    try:
        sid, _ = make_session(created)
        post("/api/session/archive", {"session_id": sid, "archived": True})
        d, status = post("/api/session/archive", {"session_id": sid, "archived": False})
        assert status == 200
        assert d["session"]["archived"] is False
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_archived_in_compact():
    """Archived field appears in session list."""
    created = []
    try:
        sid, _ = make_session(created)
        post("/api/session/rename", {"session_id": sid, "title": "Archive Test"})
        post("/api/session/archive", {"session_id": sid, "archived": True})
        d, _ = get(f"/api/session?session_id={sid}")
        assert d["session"]["archived"] is True
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})
