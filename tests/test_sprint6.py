"""Sprint 6 tests: Escape from editor, Phase D validation, HTML extraction, cron create, session export."""
import json, uuid, pathlib, urllib.parse, urllib.request, urllib.error
REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()

from tests._pytest_port import BASE

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read(), r.headers, r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def make_session_tracked(created_list, ws=None):
    body = {}
    if ws: body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(d["session"]["workspace"])

# ── Phase E: HTML served from static/index.html ──

def test_index_html_served():
    raw, headers, status = get_raw("/")
    assert status == 200
    assert b"sidebarResize" in raw, "Resize handle not found in HTML"
    assert b'id="mainTasks"' in raw, "Tasks main-view not found in HTML"
    assert b'id="settingsMenu"' in raw, "Settings left-rail menu not found in HTML"
    assert b"btnExportJSON" in raw, "Export JSON button not found in HTML"

def test_index_html_file_exists():
    p = REPO_ROOT / "static/index.html"
    assert p.exists(), "static/index.html does not exist"
    assert p.stat().st_size > 5000, "index.html seems too small"

def test_server_py_has_no_html_string():
    txt = (REPO_ROOT / "server.py").read_text()
    assert 'HTML = r"""' not in txt, "server.py still contains inline HTML string"
    assert "doctype html" not in txt.lower(), "server.py still contains raw HTML"

# ── Phase D: remaining endpoint validation ──

def test_approval_respond_requires_session_id():
    result, status = post("/api/approval/respond", {"choice": "deny"})
    assert status == 400

def test_approval_respond_rejects_invalid_choice(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/approval/respond", {"session_id": sid, "choice": "INVALID"})
    assert status == 400

def test_file_raw_requires_session_id():
    try:
        get_raw("/api/file/raw?path=test.png")
        assert False, "Expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_file_raw_unknown_session():
    try:
        get_raw("/api/file/raw?session_id=nosuchsession&path=test.png")
        assert False, "Expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404

def test_file_raw_serves_session_attachment_inbox(cleanup_test_sessions):
    from api.upload import _session_attachment_dir

    sid, workspace = make_session_tracked(cleanup_test_sessions)
    filename = f"uploaded-chat-image-{uuid.uuid4().hex}.png"
    attachment_dir = _session_attachment_dir(sid)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    payload = b"fake-png-bytes"
    (attachment_dir / filename).write_bytes(payload)

    assert not (workspace / filename).exists(), "regression must exercise attachment fallback"
    raw, headers, status = get_raw(
        f"/api/file/raw?session_id={sid}&path={urllib.parse.quote(filename)}"
    )
    assert status == 200
    assert raw == payload
    assert "image/png" in headers.get("Content-Type", "")

def test_file_raw_attachment_fallback_rejects_traversal(cleanup_test_sessions):
    from api.upload import _session_attachment_dir

    sid, _ = make_session_tracked(cleanup_test_sessions)
    attachment_dir = _session_attachment_dir(sid)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    (attachment_dir / "safe.txt").write_text("safe", encoding="utf-8")
    try:
        get_raw(f"/api/file/raw?session_id={sid}&path={urllib.parse.quote('../../safe.txt')}")
        assert False, "Expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404

# ── Cron create ──

def test_cron_create_requires_prompt():
    result, status = post("/api/crons/create", {"schedule": "0 9 * * *"})
    assert status == 400
    assert "prompt" in result.get("error", "").lower()

def test_cron_create_requires_schedule():
    result, status = post("/api/crons/create", {"prompt": "Say hello"})
    assert status == 400
    assert "schedule" in result.get("error", "").lower()

def test_cron_create_invalid_schedule():
    result, status = post("/api/crons/create", {
        "prompt": "Say hello", "schedule": "not_a_valid_schedule_xyz"
    })
    assert status == 400

def test_cron_create_success():
    uid = uuid.uuid4().hex[:6]
    result, status = post("/api/crons/create", {
        "name": f"test-job-{uid}",
        "prompt": "Just say 'hello' and nothing else.",
        "schedule": "every 999h",  # far future -- won't actually run during test
        "deliver": "local",
    })
    assert status == 200, f"Expected 200 got {status}: {result}"
    assert result["ok"] is True
    assert "job" in result
    job_id = result["job"]["id"]
    # Verify it appears in the cron list
    jobs, _ = get("/api/crons")
    ids = [j["id"] for j in jobs["jobs"]]
    assert job_id in ids, f"Created job {job_id} not in list"

# ── Session export ──

def test_session_export_requires_session_id():
    try:
        get_raw("/api/session/export")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_session_export_unknown_session():
    try:
        get_raw("/api/session/export?session_id=nosuchsession")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404

def test_session_export_returns_json(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    raw, headers, status = get_raw(f"/api/session/export?session_id={sid}")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(raw)
    assert data["session_id"] == sid
    assert "messages" in data
    assert "title" in data

# ── Resizable panels: static files present ──

def test_static_index_has_resize_handles():
    raw, _, status = get_raw("/")
    assert status == 200
    assert b"sidebarResize" in raw
    assert b"rightpanelResize" in raw

def test_app_js_has_resize_logic():
    """Sprint 9: app.js replaced by modules. Resize logic lives in boot.js."""
    raw, _, status = get_raw("/static/boot.js")
    assert status == 200
    assert b"_initResizePanels" in raw
    assert b"hermes-sidebar-w" in raw
    assert b"hermes-panel-w" in raw
