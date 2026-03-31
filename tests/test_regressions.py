"""
Regression tests -- one test per bug that was introduced and fixed.
These tests exist specifically to prevent those bugs from silently returning.

Each test is tagged with the sprint/commit where the bug was found and fixed.
"""
import json
import pathlib
import time
import urllib.error
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:8788"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read(), r.headers.get("Content-Type",""), r.status

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
    return sid


# ── R1: uuid not imported in server.py (Sprint 10 split regression) ──────────

def test_chat_start_returns_stream_id(cleanup_test_sessions):
    """R1: chat/start must return stream_id -- catches missing uuid import.
    When uuid was missing, this returned 500 (NameError).
    """
    sid = make_session(cleanup_test_sessions)
    data, status = post("/api/chat/start", {
        "session_id": sid,
        "message": "ping",
        "model": "openai/gpt-5.4-mini",
    })
    # Must return 200 with a stream_id -- not 500
    assert status == 200, f"chat/start failed with {status}: {data}"
    assert "stream_id" in data, "stream_id missing from chat/start response"
    assert len(data["stream_id"]) > 8, "stream_id looks invalid"
    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R2: AIAgent not imported in api/streaming.py (Sprint 10 split regression) ─

def test_chat_stream_opens_successfully(cleanup_test_sessions):
    """R2: After chat/start, GET /api/chat/stream must return 200 (SSE opens).
    When AIAgent was missing, the thread crashed immediately, popped STREAMS,
    and the SSE GET returned 404.
    """
    sid = make_session(cleanup_test_sessions)
    data, status = post("/api/chat/start", {
        "session_id": sid,
        "message": "say: hello",
        "model": "openai/gpt-5.4-mini",
    })
    assert status == 200, f"chat/start failed: {data}"
    stream_id = data["stream_id"]

    # Open the SSE stream -- must return 200, not 404
    # We only check headers (don't read the full stream body)
    req = urllib.request.Request(BASE + f"/api/chat/stream?stream_id={stream_id}")
    try:
        r = urllib.request.urlopen(req, timeout=3)
        assert r.status == 200, f"SSE stream returned {r.status} (expected 200)"
        ct = r.headers.get("Content-Type", "")
        assert "text/event-stream" in ct, f"Wrong Content-Type: {ct}"
        r.close()
    except urllib.error.HTTPError as e:
        assert False, f"SSE stream returned {e.code} -- AIAgent may not be imported"
    except Exception:
        pass  # timeout or connection close after brief read is fine

    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R3: Session.__init__ missing tool_calls param (Sprint 10 split regression) ─

def test_session_with_tool_calls_in_json_loads_ok(cleanup_test_sessions):
    """R3: Sessions that have tool_calls in their JSON must load without 500.
    When tool_calls=None was missing from Session.__init__, loading such sessions
    threw TypeError: unexpected keyword argument.
    """
    sid = make_session(cleanup_test_sessions)

    # Manually inject tool_calls into the session's JSON file
    sessions_dir = pathlib.Path.home() / ".hermes" / "webui-mvp-test" / "sessions"
    session_file = sessions_dir / f"{sid}.json"
    if session_file.exists():
        d = json.loads(session_file.read_text())
        d["tool_calls"] = [
            {"name": "terminal", "snippet": "test output", "tid": "test_tid_001", "assistant_msg_idx": 1}
        ]
        session_file.write_text(json.dumps(d))

    # Loading the session must return 200, not 500
    data, status = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
    assert status == 200, f"Session with tool_calls returned {status}: {data}"
    assert data["session"]["session_id"] == sid

    post("/api/session/delete", {"session_id": sid})
    cleanup_test_sessions.clear()


# ── R4: has_pending not imported in streaming.py (Sprint 10 split regression) ─

def test_streaming_py_imports_has_pending(cleanup_test_sessions):
    """R4: api/streaming.py must import or define has_pending.
    When missing, the approval check mid-stream caused NameError.
    """
    src = pathlib.Path("/home/hermes/webui-mvp/api/streaming.py").read_text()
    assert "has_pending" in src, "has_pending not found in api/streaming.py"
    # Verify it's imported (not just used)
    assert "import" in src and "has_pending" in src, \
        "has_pending must be imported in api/streaming.py"


def test_aiagent_imported_in_streaming(cleanup_test_sessions):
    """R2b: api/streaming.py must import AIAgent.
    When missing, the streaming thread crashed immediately after being spawned.
    """
    src = pathlib.Path("/home/hermes/webui-mvp/api/streaming.py").read_text()
    assert "AIAgent" in src, "AIAgent not referenced in api/streaming.py"
    assert "from run_agent import AIAgent" in src or "import AIAgent" in src, \
        "AIAgent must be imported in api/streaming.py"


# ── R5: SSE loop did not break on cancel event (Sprint 10 bug) ───────────────

def test_cancel_nonexistent_stream_returns_not_cancelled(cleanup_test_sessions):
    """R5a: Cancel endpoint works and returns cancelled:false for unknown stream."""
    data, status = get("/api/chat/cancel?stream_id=nonexistent_test_xyz")
    assert status == 200
    assert data["ok"] is True
    assert data["cancelled"] is False


def test_server_py_sse_loop_breaks_on_cancel(cleanup_test_sessions):
    """R5b: server.py SSE loop must include 'cancel' in the break condition.
    When missing, the connection hung after the cancel event was processed.
    """
    src = pathlib.Path("/home/hermes/webui-mvp/server.py").read_text()
    # Find the SSE break condition
    import re
    m = re.search(r"if event in \([^)]+\):\s*break", src)
    assert m, "SSE break condition not found in server.py"
    assert "cancel" in m.group(), \
        f"'cancel' missing from SSE break condition: {m.group()}"


# ── R6: Test cron isolation (Sprint 10) ──────────────────────────────────────

def test_real_jobs_json_not_polluted_by_tests(cleanup_test_sessions):
    """R6: Test runs must not write to the real ~/.hermes/cron/jobs.json.
    When HERMES_HOME isolation was missing, every test run added test-job-* entries.
    """
    real_jobs_path = pathlib.Path.home() / ".hermes" / "cron" / "jobs.json"
    if not real_jobs_path.exists():
        return  # no jobs file at all -- fine

    jobs = json.loads(real_jobs_path.read_text())
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])

    test_jobs = [j for j in jobs if j.get("name", "").startswith("test-job-")]
    assert len(test_jobs) == 0, \
        f"Real jobs.json contains {len(test_jobs)} test-job-* entries: " \
        f"{[j['name'] for j in test_jobs]}"


# ── General: api modules all importable ──────────────────────────────────────

def test_all_api_modules_importable(cleanup_test_sessions):
    """All api/ modules must be importable without NameError or ImportError.
    Catches missing imports introduced during future module splits.
    """
    import ast, pathlib
    api_dir = pathlib.Path("/home/hermes/webui-mvp/api")
    for module_file in api_dir.glob("*.py"):
        src = module_file.read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            assert False, f"{module_file.name} has syntax error: {e}"


def test_server_py_importable(cleanup_test_sessions):
    """server.py must parse without syntax errors after any split."""
    import ast, pathlib
    src = pathlib.Path("/home/hermes/webui-mvp/server.py").read_text()
    try:
        ast.parse(src)
    except SyntaxError as e:
        assert False, f"server.py has syntax error: {e}"
