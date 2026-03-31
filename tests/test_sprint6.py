     1|"""Sprint 6 tests: Escape from editor, Phase D validation, HTML extraction, cron create, session export."""
from conftest import REPO_ROOT
     2|import json, uuid, pathlib, urllib.request, urllib.error
     3|
     4|BASE = "http://127.0.0.1:8788"  # isolated test server
     5|
     6|def get(path):
     7|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
     8|        return json.loads(r.read()), r.status
     9|
    10|def get_raw(path):
    11|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
    12|        return r.read(), r.headers, r.status
    13|
    14|def post(path, body=None):
    15|    data = json.dumps(body or {}).encode()
    16|    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    17|    try:
    18|        with urllib.request.urlopen(req, timeout=10) as r:
    19|            return json.loads(r.read()), r.status
    20|    except urllib.error.HTTPError as e:
    21|        return json.loads(e.read()), e.code
    22|
    23|def make_session_tracked(created_list, ws=None):
    24|    body = {}
    25|    if ws: body["workspace"] = str(ws)
    26|    d, _ = post("/api/session/new", body)
    27|    sid = d["session"]["session_id"]
    28|    created_list.append(sid)
    29|    return sid, pathlib.Path(d["session"]["workspace"])
    30|
    31|# ── Phase E: HTML served from static/index.html ──
    32|
    33|def test_index_html_served():
    34|    raw, headers, status = get_raw("/")
    35|    assert status == 200
    36|    assert b"sidebarResize" in raw, "Resize handle not found in HTML"
    37|    assert b"cronCreateForm" in raw, "Cron create form not found in HTML"
    38|    assert b"btnExportJSON" in raw, "Export JSON button not found in HTML"
    39|
    40|def test_index_html_file_exists():
    41|    p = REPO_ROOT / "static/index.html"
    42|    assert p.exists(), "static/index.html does not exist"
    43|    assert p.stat().st_size > 5000, "index.html seems too small"
    44|
    45|def test_server_py_has_no_html_string():
    46|    txt = REPO_ROOT / "server.py".read_text()
    47|    assert 'HTML = r"""' not in txt, "server.py still contains inline HTML string"
    48|    assert "doctype html" not in txt.lower(), "server.py still contains raw HTML"
    49|
    50|# ── Phase D: remaining endpoint validation ──
    51|
    52|def test_approval_respond_requires_session_id():
    53|    result, status = post("/api/approval/respond", {"choice": "deny"})
    54|    assert status == 400
    55|
    56|def test_approval_respond_rejects_invalid_choice(cleanup_test_sessions):
    57|    sid, _ = make_session_tracked(cleanup_test_sessions)
    58|    result, status = post("/api/approval/respond", {"session_id": sid, "choice": "INVALID"})
    59|    assert status == 400
    60|
    61|def test_file_raw_requires_session_id():
    62|    try:
    63|        get_raw("/api/file/raw?path=test.png")
    64|        assert False, "Expected 400"
    65|    except urllib.error.HTTPError as e:
    66|        assert e.code == 400
    67|
    68|def test_file_raw_unknown_session():
    69|    try:
    70|        get_raw("/api/file/raw?session_id=nosuchsession&path=test.png")
    71|        assert False, "Expected 404"
    72|    except urllib.error.HTTPError as e:
    73|        assert e.code == 404
    74|
    75|# ── Cron create ──
    76|
    77|def test_cron_create_requires_prompt():
    78|    result, status = post("/api/crons/create", {"schedule": "0 9 * * *"})
    79|    assert status == 400
    80|    assert "prompt" in result.get("error", "").lower()
    81|
    82|def test_cron_create_requires_schedule():
    83|    result, status = post("/api/crons/create", {"prompt": "Say hello"})
    84|    assert status == 400
    85|    assert "schedule" in result.get("error", "").lower()
    86|
    87|def test_cron_create_invalid_schedule():
    88|    result, status = post("/api/crons/create", {
    89|        "prompt": "Say hello", "schedule": "not_a_valid_schedule_xyz"
    90|    })
    91|    assert status == 400
    92|
    93|def test_cron_create_success():
    94|    uid = uuid.uuid4().hex[:6]
    95|    result, status = post("/api/crons/create", {
    96|        "name": f"test-job-{uid}",
    97|        "prompt": "Just say 'hello' and nothing else.",
    98|        "schedule": "every 999h",  # far future -- won't actually run during test
    99|        "deliver": "local",
   100|    })
   101|    assert status == 200, f"Expected 200 got {status}: {result}"
   102|    assert result["ok"] is True
   103|    assert "job" in result
   104|    job_id = result["job"]["id"]
   105|    # Verify it appears in the cron list
   106|    jobs, _ = get("/api/crons")
   107|    ids = [j["id"] for j in jobs["jobs"]]
   108|    assert job_id in ids, f"Created job {job_id} not in list"
   109|
   110|# ── Session export ──
   111|
   112|def test_session_export_requires_session_id():
   113|    try:
   114|        get_raw("/api/session/export")
   115|        assert False
   116|    except urllib.error.HTTPError as e:
   117|        assert e.code == 400
   118|
   119|def test_session_export_unknown_session():
   120|    try:
   121|        get_raw("/api/session/export?session_id=nosuchsession")
   122|        assert False
   123|    except urllib.error.HTTPError as e:
   124|        assert e.code == 404
   125|
   126|def test_session_export_returns_json(cleanup_test_sessions):
   127|    sid, _ = make_session_tracked(cleanup_test_sessions)
   128|    raw, headers, status = get_raw(f"/api/session/export?session_id={sid}")
   129|    assert status == 200
   130|    assert "application/json" in headers.get("Content-Type", "")
   131|    data = json.loads(raw)
   132|    assert data["session_id"] == sid
   133|    assert "messages" in data
   134|    assert "title" in data
   135|
   136|# ── Resizable panels: static files present ──
   137|
   138|def test_static_index_has_resize_handles():
   139|    raw, _, status = get_raw("/")
   140|    assert status == 200
   141|    assert b"sidebarResize" in raw
   142|    assert b"rightpanelResize" in raw
   143|
   144|def test_app_js_has_resize_logic():
   145|    """Sprint 9: app.js replaced by modules. Resize logic lives in boot.js."""
   146|    raw, _, status = get_raw("/static/boot.js")
   147|    assert status == 200
   148|    assert b"_initResizePanels" in raw
   149|    assert b"hermes-sidebar-w" in raw
   150|    assert b"hermes-panel-w" in raw
   151|