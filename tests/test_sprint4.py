"""Sprint 4 tests: relocation, session rename, search, file ops, validation."""
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

def test_static_css_served():
    raw, ct, status = get_raw("/static/style.css")
    assert status == 200 and "text/css" in ct and b"--bg" in raw

def test_static_unknown_file_404():
    try:
        get_raw("/static/doesnotexist.xyz")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404

def test_session_rename(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/session/rename", {"session_id": sid, "title": "Renamed Session"})
    assert status == 200 and result["session"]["title"] == "Renamed Session"

def test_session_rename_persists(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/rename", {"session_id": sid, "title": "Persisted"})
    loaded, _ = get(f"/api/session?session_id={sid}")
    assert loaded["session"]["title"] == "Persisted"

def test_session_rename_truncates(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/session/rename", {"session_id": sid, "title": "A" * 200})
    assert status == 200 and len(result["session"]["title"]) <= 80

def test_session_rename_requires_fields():
    result, status = post("/api/session/rename", {"session_id": "x"})
    assert status == 400
    result2, status2 = post("/api/session/rename", {"title": "hi"})
    assert status2 == 400

def test_session_rename_unknown_id():
    result, status = post("/api/session/rename", {"session_id": "nosuchid", "title": "hi"})
    assert status == 404

def test_session_search_returns_matches(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    uid = uuid.uuid4().hex[:8]
    post("/api/session/rename", {"session_id": sid, "title": f"s4-search-{uid}"})
    data, status = get(f"/api/sessions/search?q=s4-search-{uid}")
    assert status == 200
    sids = [s["session_id"] for s in data["sessions"]]
    assert sid in sids

def test_session_search_empty_query_returns_all():
    data, status = get("/api/sessions/search?q=")
    assert status == 200 and "sessions" in data

def test_session_search_no_results():
    data, status = get("/api/sessions/search?q=zzznomatchzzz9999")
    assert status == 200 and data["sessions"] == []

def test_file_create(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    fname = f"test_{uuid.uuid4().hex[:6]}.txt"
    result, status = post("/api/file/create", {"session_id": sid, "path": fname, "content": "hello sprint4"})
    assert status == 200 and result["ok"] is True
    assert (ws / fname).read_text() == "hello sprint4"

def test_file_create_requires_fields(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/create", {"session_id": sid})
    assert status == 400
    result2, status2 = post("/api/file/create", {"path": "x.txt"})
    assert status2 == 400

def test_file_create_duplicate_rejected(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    fname = f"dup_{uuid.uuid4().hex[:6]}.txt"
    post("/api/file/create", {"session_id": sid, "path": fname, "content": ""})
    result, status = post("/api/file/create", {"session_id": sid, "path": fname, "content": ""})
    assert status == 400

def test_file_delete(cleanup_test_sessions):
    sid, ws = make_session_tracked(cleanup_test_sessions)
    (ws / "to_delete.txt").write_text("bye")
    result, status = post("/api/file/delete", {"session_id": sid, "path": "to_delete.txt"})
    assert status == 200 and not (ws / "to_delete.txt").exists()

def test_file_delete_missing_returns_404(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/delete", {"session_id": sid, "path": "nosuchfile.txt"})
    assert status == 404

def test_file_delete_path_traversal_blocked(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/file/delete", {"session_id": sid, "path": "../../etc/passwd"})
    assert status in (400, 500)

def test_list_requires_session_id():
    try:
        get("/api/list?path=.")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_file_requires_session_id():
    try:
        get("/api/file?path=readme.txt")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_file_requires_path(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    try:
        get(f"/api/file?session_id={sid}")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_new_session_inherits_workspace(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/update", {"session_id": sid, "workspace": "/tmp", "model": "openai/gpt-5.4-mini"})
    sid2, _ = make_session_tracked(cleanup_test_sessions)
    data, _ = get(f"/api/session?session_id={sid2}")
    assert data["session"]["workspace"] == "/tmp"
