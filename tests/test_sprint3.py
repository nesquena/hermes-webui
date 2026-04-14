"""Sprint 3 tests: cron API, skills API, memory API, input validation."""
import json, uuid, urllib.request, urllib.error

BASE = "http://127.0.0.1:8788"  # test server (isolated from production)

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

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


def test_crons_list():
    data, status = get("/api/crons")
    assert status == 200
    assert "jobs" in data

def test_crons_list_has_required_fields():
    data, _ = get("/api/crons")
    if not data["jobs"]: return
    job = data["jobs"][0]
    for field in ("id", "name", "prompt", "enabled", "schedule_display"):
        assert field in job

def test_crons_output_requires_job_id():
    try:
        get("/api/crons/output")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_crons_output_real_job():
    data, _ = get("/api/crons")
    if not data["jobs"]: return
    job_id = data["jobs"][0]["id"]
    out, status = get(f"/api/crons/output?job_id={job_id}&limit=3")
    assert status == 200
    assert "outputs" in out

def test_crons_pause_requires_job_id():
    result, status = post("/api/crons/pause", {})
    assert status in (400, 404)

def test_crons_resume_requires_job_id():
    result, status = post("/api/crons/resume", {})
    assert status in (400, 404)

def test_crons_run_nonexistent():
    result, status = post("/api/crons/run", {"job_id": "doesnotexist999"})
    assert status == 404

def test_skills_list():
    data, status = get("/api/skills")
    assert status == 200
    assert len(data["skills"]) > 0

def test_skills_list_has_required_fields():
    data, _ = get("/api/skills")
    skill = data["skills"][0]
    assert "name" in skill and "description" in skill

def test_skills_content_known():
    data, status = get("/api/skills/content?name=dogfood")
    assert status == 200
    assert len(data["content"]) > 0

def test_skills_content_requires_name():
    try:
        get("/api/skills/content")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_skills_search_returns_subset():
    data, _ = get("/api/skills")
    assert len(data["skills"]) > 5

def test_memory_returns_both_files():
    data, status = get("/api/memory")
    assert status == 200
    assert "memory" in data and "user" in data

def test_memory_content_is_string():
    data, _ = get("/api/memory")
    assert isinstance(data["memory"], str)
    assert isinstance(data["user"], str)

def test_memory_has_mtime():
    data, _ = get("/api/memory")
    assert "memory_mtime" in data and "user_mtime" in data

def test_session_update_requires_session_id():
    result, status = post("/api/session/update", {"model": "openai/gpt-5.4-mini"})
    assert status == 400

def test_session_delete_requires_session_id():
    result, status = post("/api/session/delete", {})
    assert status == 400

def test_chat_start_requires_session_id():
    result, status = post("/api/chat/start", {"message": "hello"})
    assert status == 400

def test_chat_start_requires_message(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/chat/start", {"session_id": sid, "message": ""})
    assert status == 400

def test_session_update_unknown_id_returns_404():
    result, status = post("/api/session/update", {"session_id": "nosuchsession", "model": "openai/gpt-5.4-mini"})
    assert status == 404


def test_session_update_rejects_workspace_outside_trusted_root(tmp_path):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    result, status = post("/api/session/update", {"session_id": sid, "workspace": str(outside)})
    assert status == 400
    assert "trusted workspace root" in result.get("error", "").lower()


def test_chat_start_rejects_workspace_outside_trusted_root(tmp_path):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    outside = tmp_path / "outside-chat"
    outside.mkdir(parents=True, exist_ok=True)
    result, status = post("/api/chat/start", {"session_id": sid, "message": "hello", "workspace": str(outside)})
    assert status == 400
    assert "trusted workspace root" in result.get("error", "").lower()


def test_workspace_add_rejects_path_outside_trusted_root(tmp_path):
    outside = tmp_path / "outside-add"
    outside.mkdir(parents=True, exist_ok=True)
    result, status = post("/api/workspaces/add", {"path": str(outside), "name": "Outside"})
    assert status == 400
    assert "trusted workspace root" in result.get("error", "").lower()


def test_session_new_rejects_workspace_outside_trusted_root(tmp_path):
    outside = tmp_path / "outside-new"
    outside.mkdir(parents=True, exist_ok=True)
    result, status = post("/api/session/new", {"workspace": str(outside)})
    assert status == 400
    assert "trusted workspace root" in result.get("error", "").lower()


def test_session_search_returns_matches(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/rename", {"session_id": sid, "title": f"unique-s3-{sid}"})
    data, status = get(f"/api/sessions/search?q=unique-s3-{sid}")
    assert status == 200
    sids = [s["session_id"] for s in data["sessions"]]
    assert sid in sids

def test_session_search_empty_query_returns_all():
    data, status = get("/api/sessions/search?q=")
    assert status == 200 and "sessions" in data

def test_session_search_no_results():
    data, status = get("/api/sessions/search?q=zzznomatchzzz9999")
    assert status == 200 and data["sessions"] == []
