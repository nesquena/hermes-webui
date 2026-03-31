     1|"""
from conftest import REPO_ROOT
     2|Sprint 10 Tests: server.py split, cancel endpoint, cron history, tool card polish.
     3|"""
     4|import json, pathlib, urllib.error, urllib.request, urllib.parse
     5|
     6|BASE = "http://127.0.0.1:8788"
     7|
     8|def get(path):
     9|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
    10|        return json.loads(r.read()), r.status
    11|
    12|def get_text(path):
    13|    with urllib.request.urlopen(BASE + path, timeout=10) as r:
    14|        return r.read().decode(), r.status
    15|
    16|def post(path, body=None):
    17|    data = json.dumps(body or {}).encode()
    18|    req = urllib.request.Request(BASE + path, data=data,
    19|                                  headers={"Content-Type": "application/json"})
    20|    try:
    21|        with urllib.request.urlopen(req, timeout=10) as r:
    22|            return json.loads(r.read()), r.status
    23|    except urllib.error.HTTPError as e:
    24|        return json.loads(e.read()), e.code
    25|
    26|def make_session(created_list):
    27|    d, _ = post("/api/session/new", {})
    28|    sid = d["session"]["session_id"]
    29|    created_list.append(sid)
    30|    return sid
    31|
    32|# ── server.py split: api/ modules served / importable ─────────────────────
    33|
    34|def test_health_still_works(cleanup_test_sessions):
    35|    data, status = get("/health")
    36|    assert status == 200
    37|    assert data["status"] == "ok"
    38|    assert "uptime_seconds" in data
    39|    assert "active_streams" in data
    40|
    41|def test_api_modules_exist(cleanup_test_sessions):
    42|    """All api/ module files must exist on disk."""
    43|    base = REPO_ROOT / "api"
    44|    for mod in ["__init__.py", "config.py", "helpers.py", "models.py",
    45|                "workspace.py", "upload.py", "streaming.py"]:
    46|        assert (base / mod).exists(), f"Missing api/{mod}"
    47|
    48|def test_server_py_under_700_lines(cleanup_test_sessions):
    49|    """server.py should be under 700 lines after the split."""
    50|    lines = len(REPO_ROOT / "server.py".read_text().splitlines())
    51|    assert lines < 700, f"server.py is {lines} lines -- split may not have landed"
    52|
    53|def test_api_config_has_cancel_flags(cleanup_test_sessions):
    54|    src = REPO_ROOT / "api/config.py".read_text()
    55|    assert "CANCEL_FLAGS" in src
    56|    assert "STREAMS" in src
    57|
    58|def test_session_crud_still_works(cleanup_test_sessions):
    59|    """Full session lifecycle works after split."""
    60|    created = []
    61|    sid = make_session(created)
    62|    data, status = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
    63|    assert status == 200
    64|    assert data["session"]["session_id"] == sid
    65|    post("/api/session/delete", {"session_id": sid})
    66|
    67|def test_static_files_still_served(cleanup_test_sessions):
    68|    for f in ["ui.js", "workspace.js", "sessions.js", "messages.js", "panels.js", "boot.js"]:
    69|        src, status = get_text(f"/static/{f}")
    70|        assert status == 200, f"/static/{f} returned {status}"
    71|        assert len(src) > 100
    72|
    73|# ── Cancel endpoint ────────────────────────────────────────────────────────
    74|
    75|def test_cancel_requires_stream_id(cleanup_test_sessions):
    76|    try:
    77|        data, status = get("/api/chat/cancel")
    78|        assert status == 400
    79|    except urllib.error.HTTPError as e:
    80|        assert e.code == 400
    81|
    82|def test_cancel_nonexistent_stream(cleanup_test_sessions):
    83|    data, status = get("/api/chat/cancel?stream_id=nonexistent_xyz")
    84|    assert status == 200
    85|    assert data["ok"] is True
    86|    assert data["cancelled"] is False
    87|
    88|def test_cancel_button_in_html(cleanup_test_sessions):
    89|    src, _ = get_text("/")
    90|    assert "btnCancel" in src
    91|    assert "cancelStream" in src
    92|
    93|def test_cancel_function_in_boot_js(cleanup_test_sessions):
    94|    src, _ = get_text("/static/boot.js")
    95|    assert "async function cancelStream(" in src
    96|    assert "/api/chat/cancel" in src
    97|
    98|# ── Cron history ───────────────────────────────────────────────────────────
    99|
   100|def test_crons_output_limit_param(cleanup_test_sessions):
   101|    """Server accepts limit parameter > 1."""
   102|    data, status = get("/api/crons/output?job_id=nonexistent&limit=20")
   103|    # 404 or 200 with empty -- both acceptable for nonexistent job
   104|    assert status in (200, 404)
   105|
   106|def test_cron_history_button_in_panels_js(cleanup_test_sessions):
   107|    src, _ = get_text("/static/panels.js")
   108|    assert "loadCronHistory" in src
   109|    assert "All runs" in src
   110|
   111|def test_cron_output_snippet_helper(cleanup_test_sessions):
   112|    src, _ = get_text("/static/panels.js")
   113|    assert "_cronOutputSnippet" in src
   114|
   115|# ── Tool card polish ───────────────────────────────────────────────────────
   116|
   117|def test_tool_card_running_dot_in_css(cleanup_test_sessions):
   118|    src, _ = get_text("/static/style.css")
   119|    assert "tool-card-running-dot" in src
   120|
   121|def test_tool_card_show_more_in_ui_js(cleanup_test_sessions):
   122|    src, _ = get_text("/static/ui.js")
   123|    assert "Show more" in src
   124|    assert "tool-card-more" in src
   125|
   126|def test_tool_card_smart_truncation_in_ui_js(cleanup_test_sessions):
   127|    src, _ = get_text("/static/ui.js")
   128|    assert "displaySnippet" in src
   129|    assert "lastBreak" in src
   130|
   131|def test_cancel_sse_event_handler_in_messages_js(cleanup_test_sessions):
   132|    src, _ = get_text("/static/messages.js")
   133|    assert "addEventListener('cancel'" in src
   134|    assert "Task cancelled" in src
   135|
   136|def test_active_stream_id_tracked(cleanup_test_sessions):
   137|    src, _ = get_text("/static/messages.js")
   138|    assert "S.activeStreamId" in src
   139|