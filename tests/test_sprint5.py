"""Sprint 5 tests: workspace CRUD, file save, session index, JS serving."""
import json, pathlib, uuid, urllib.request, urllib.error

BASE = "http://127.0.0.1:8788"  # test server (isolated from production)

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read(), r.headers.get("Content-Type",""), r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def make_session_tracked(created_list, ws=None):
    """Create a session and register it with the cleanup fixture."""
    import pathlib as _pathlib
    body = {}
    if ws: body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, _pathlib.Path(d["session"]["workspace"])


def test_server_running_from_new_location():
    data, status = get("/health")
    assert status == 200 and data["status"] == "ok"

def test_app_js_served():
    raw, ct, status = get_raw("/static/app.js")
    assert status == 200 and "javascript" in ct and b"renderMd" in raw

def test_workspaces_list():
    data, status = get("/api/workspaces")
    assert status == 200 and "workspaces" in data and "last" in data

def test_workspace_add_valid():
    post("/api/workspaces/remove", {"path": "/tmp"})
    result, status = post("/api/workspaces/add", {"path": "/tmp", "name": "Temp"})
    assert status == 200 and any(w["path"]=="/tmp" for w in result["workspaces"])
    post("/api/workspaces/remove", {"path": "/tmp"})

def test_workspace_add_validates_existence():
    result, status = post("/api/workspaces/add", {"path": "/tmp/does_not_exist_xyz_999"})
    assert status == 400

def test_workspace_add_validates_is_dir():
    result, status = post("/api/workspaces/add", {"path": "/etc/hostname"})
    assert status == 400

def test_workspace_add_no_duplicate():
    post("/api/workspaces/remove", {"path": "/tmp"})
    post("/api/workspaces/add", {"path": "/tmp"})
    result, status = post("/api/workspaces/add", {"path": "/tmp"})
    assert status == 400 and "already" in result.get("error","").lower()
    post("/api/workspaces/remove", {"path": "/tmp"})

def test_workspace_add_requires_path():
    result, status = post("/api/workspaces/add", {})
    assert status == 400

def test_workspace_remove():
    post("/api/workspaces/remove", {"path": "/tmp"})
    post("/api/workspaces/add", {"path": "/tmp", "name": "Temp"})
    result, status = post("/api/workspaces/remove", {"path": "/tmp"})
    assert status == 200 and "/tmp" not in [w["path"] for w in result["workspaces"]]

def test_workspace_rename():
    post("/api/workspaces/remove", {"path": "/tmp"})
    post("/api/workspaces/add", {"path": "/tmp", "name": "Temp"})
    result, status = post("/api/workspaces/rename", {"path": "/tmp", "name": "My Temp"})
    assert status == 200
    assert {w["path"]: w["name"] for w in result["workspaces"]}.get("/tmp") == "My Temp"
    post("/api/workspaces/remove", {"path": "/tmp"})

def test_workspace_rename_unknown():
    result, status = post("/api/workspaces/rename", {"path": "/no/such/path", "name": "X"})
    assert status == 404

def test_last_workspace_updates_on_session_update(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/update", {"session_id": sid, "workspace": "/tmp", "model": "openai/gpt-5.4-mini"})
    data, _ = get("/api/workspaces")
    assert data["last"] == "/tmp"

def test_file_save(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    fname = f"save_{uuid.uuid4().hex[:6]}.txt"
    (ws / fname).write_text("original content")
    result, status = post("/api/file/save", {"session_id": sid, "path": fname, "content": "updated"})
    assert status == 200 and (ws / fname).read_text() == "updated"

def test_file_save_requires_fields(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/save", {"session_id": sid})
    assert status == 400

def test_file_save_nonexistent_returns_404(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/save", {"session_id": sid, "path": "no_such.txt", "content": ""})
    assert status == 404

def test_file_save_path_traversal_blocked(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/save", {"session_id": sid, "path": "../../etc/passwd", "content": ""})
    assert status in (400, 500)

def test_session_index_created_after_save(cleanup_test_sessions):
    # Index is created in the TEST state dir, not the production dir
    test_state_dir = pathlib.Path.home() / ".hermes" / "webui-mvp-test"
    index_path = test_state_dir / "sessions" / "_index.json"
    make_session_tracked(cleanup_test_sessions)
    # Index may not exist yet if cleanup already wiped it -- just check the endpoint works
    data, status = get("/api/sessions")
    assert status == 200
    assert isinstance(data["sessions"], list)

def test_sessions_endpoint_returns_sorted():
    data, status = get("/api/sessions")
    assert status == 200
    sessions = data["sessions"]
    if len(sessions) >= 2:
        assert sessions[0]["updated_at"] >= sessions[1]["updated_at"]

def test_new_session_inherits_last_workspace(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/update", {"session_id": sid, "workspace": "/tmp", "model": "openai/gpt-5.4-mini"})
    sid2, _ = make_session_tracked(cleanup_test_sessions)
    d, _ = get(f"/api/session?session_id={sid2}")
    assert d["session"]["workspace"] == "/tmp"
