"""
Sprint 13 Tests: cron recent endpoint, session duplicate, background alerts.
"""
import json, pathlib, urllib.error, urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"})
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


# ── Cron recent endpoint ──────────────────────────────────────────────────

def test_crons_recent_returns_200():
    """GET /api/crons/recent returns completions list."""
    d, status = get("/api/crons/recent?since=0")
    assert status == 200
    assert 'completions' in d
    assert isinstance(d['completions'], list)
    assert 'since' in d

def test_crons_recent_with_future_since():
    """Completions list is empty when since is in the future."""
    import time
    d, _ = get(f"/api/crons/recent?since={time.time() + 99999}")
    assert d['completions'] == []

def test_crons_recent_default_since():
    """Default since=0 returns all completions."""
    d, status = get("/api/crons/recent")
    assert status == 200
    assert 'completions' in d


# ── Session duplicate ─────────────────────────────────────────────────────

def test_duplicate_session():
    """Duplicating a session creates a new one with same workspace/model."""
    created = []
    try:
        sid, sess = make_session(created)
        # Set a specific model on the session
        post("/api/session/update", {
            "session_id": sid, "model": "test/dup-model",
            "workspace": sess["workspace"]
        })
        # Duplicate: create new session with same workspace/model
        d2, status = post("/api/session/new", {
            "workspace": sess["workspace"], "model": "test/dup-model"
        })
        assert status == 200
        new_sid = d2["session"]["session_id"]
        created.append(new_sid)
        assert new_sid != sid
        assert d2["session"]["model"] == "test/dup-model"
        assert d2["session"]["workspace"] == sess["workspace"]
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


# ── Session pinned field preserved across operations ──────────────────────

def test_pinned_survives_update():
    """Pinned status survives session update."""
    created = []
    try:
        sid, sess = make_session(created)
        post("/api/session/pin", {"session_id": sid, "pinned": True})
        # Update workspace/model
        post("/api/session/update", {
            "session_id": sid, "model": "test/other",
            "workspace": sess["workspace"]
        })
        d, _ = get(f"/api/session?session_id={sid}")
        assert d["session"]["pinned"] is True
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


# ── Workspace symlink validation ──────────────────────────────────────────

def test_workspace_add_rejects_nonexistent():
    """Adding a non-existent path returns 400."""
    d, status = post("/api/workspaces/add", {"path": "/nonexistent/path/12345"})
    assert status == 400

def test_workspace_add_accepts_real_dir():
    """Adding a real directory under the trusted workspace root succeeds."""
    d, _ = post("/api/session/new", {})
    root = pathlib.Path(d["session"]["workspace"])
    tmp = root / "trusted-add-test"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        d, status = post("/api/workspaces/add", {"path": str(tmp), "name": "test-ws"})
        assert status == 200
        assert d["ok"] is True
    finally:
        post("/api/workspaces/remove", {"path": str(tmp)})
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
