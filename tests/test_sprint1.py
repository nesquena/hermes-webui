"""
Sprint 1 test suite for the Hermes Co-Work Web UI.

Tests use the ISOLATED test server running on http://127.0.0.1:8788.
Production server (port 8787) and your real conversations are never touched.
Start the server before running:
    /home/hermes/.hermes/hermes-agent/webui-mvp/start.sh
    # wait 2 seconds
    pytest webui-mvp/tests/test_sprint1.py -v

All tests are HTTP-level: they call real API endpoints and verify responses.
No mocking required for session CRUD, upload parser, or approval API.
"""

import io
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
import tempfile
import pathlib

# Allow importing server modules directly for unit tests
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

BASE = "http://127.0.0.1:8788"  # test server (isolated from production)


# ──────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────

def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def post_multipart(path, fields, files):
    """Post a multipart/form-data request. files: {name: (filename, bytes)}"""
    boundary = uuid.uuid4().hex.encode()
    body = b""
    for name, value in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
        body += value.encode() + b"\r\n"
    for name, (filename, data) in files.items():
        body += b"--" + boundary + b"\r\n"
        body += f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n".encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    req = urllib.request.Request(BASE + path, data=body,
          headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session_tracked(created_list, ws=None):
    """Create a session and register it with the cleanup fixture."""
    body = {}
    if ws: body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(d["session"]["workspace"])



# ──────────────────────────────────────────────
# Health check (prerequisite for all tests)
# ──────────────────────────────────────────────

def test_health():
    """Server must be running and healthy."""
    data = get("/health")
    assert data["status"] == "ok", f"health not ok: {data}"


# ──────────────────────────────────────────────
# B11: /api/session GET footgun fix
# ──────────────────────────────────────────────

def test_session_get_no_id_returns_400():
    """B11: GET /api/session with no session_id must return 400, not silently create."""
    try:
        data = get("/api/session")
        # If we get here, the server returned 200 (old broken behavior)
        assert False, f"Expected 400 but got 200: {data}"
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"Expected 400, got {e.code}"
        body = json.loads(e.read())
        assert "error" in body


# ──────────────────────────────────────────────
# Session CRUD
# ──────────────────────────────────────────────

def test_session_create_and_load():
    """Create a session, verify it appears in /api/sessions, load it."""
    data, status = post("/api/session/new", {"model": "openai/gpt-5.4-mini"})
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert "session" in data
    sid = data["session"]["session_id"]
    assert len(sid) == 12  # uuid4().hex[:12]

    # Give it a title so it's visible in the session list (empty Untitled sessions are filtered)
    post("/api/session/rename", {"session_id": sid, "title": "test-create-verify"})

    # Verify it appears in /api/sessions list
    sessions = get("/api/sessions")
    sids = [s["session_id"] for s in sessions["sessions"]]
    assert sid in sids, f"New session {sid} not in sessions list"

    # Load it directly
    loaded = get(f"/api/session?session_id={sid}")
    assert loaded["session"]["session_id"] == sid
    assert loaded["session"]["messages"] == []

    # Cleanup
    post("/api/session/delete", {"session_id": sid})


def test_session_update():
    """Create session, update workspace and model, verify persisted."""
    data, _ = post("/api/session/new", {})
    sid = data["session"]["session_id"]

    updated, status = post("/api/session/update", {
        "session_id": sid,
        "workspace": "/tmp",
        "model": "anthropic/claude-sonnet-4.6"
    })
    assert status == 200
    assert updated["session"]["model"] == "anthropic/claude-sonnet-4.6"

    # Reload and verify persistence
    reloaded = get(f"/api/session?session_id={sid}")
    assert reloaded["session"]["model"] == "anthropic/claude-sonnet-4.6"


def test_session_delete():
    """Create session, delete it, verify it no longer loads."""
    data, _ = post("/api/session/new", {})
    sid = data["session"]["session_id"]

    result, status = post("/api/session/delete", {"session_id": sid})
    assert status == 200
    assert result.get("ok") is True

    # Trying to load it should now 404/500 (KeyError -> 500 in current handler)
    try:
        get(f"/api/session?session_id={sid}")
        assert False, "Expected error loading deleted session"
    except urllib.error.HTTPError as e:
        assert e.code in (404, 500), f"Expected 404 or 500, got {e.code}"


def test_session_delete_nonexistent():
    """Deleting a nonexistent session should return ok:True (idempotent)."""
    result, status = post("/api/session/delete", {"session_id": "doesnotexist"})
    assert status == 200
    assert result.get("ok") is True


def test_sessions_list_sorted():
    """Sessions list should be sorted most-recently-updated first."""
    # Create two sessions with a title so they're visible (empty Untitled sessions are filtered)
    a, _ = post("/api/session/new", {})
    time.sleep(0.05)
    b, _ = post("/api/session/new", {})
    sid_a = a["session"]["session_id"]
    sid_b = b["session"]["session_id"]
    post("/api/session/rename", {"session_id": sid_a, "title": "test-sort-a"})
    time.sleep(0.05)
    post("/api/session/rename", {"session_id": sid_b, "title": "test-sort-b"})

    sessions = get("/api/sessions")
    sids = [s["session_id"] for s in sessions["sessions"]]

    # b was updated more recently, should appear before a
    assert sids.index(sid_b) < sids.index(sid_a), \
        "Sessions not sorted by updated_at desc"

    # Cleanup
    post("/api/session/delete", {"session_id": sid_a})
    post("/api/session/delete", {"session_id": sid_b})


# ──────────────────────────────────────────────
# Upload parser unit tests (pure function, no HTTP)
# ──────────────────────────────────────────────

def test_parse_multipart_text_file():
    """parse_multipart correctly parses a text file field."""
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
    # Import the function directly from the server module
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "server",
        str(pathlib.Path(__file__).parent.parent / "server.py")
    )
    # We only need parse_multipart; import it without running the server
    # Parse manually by reading the source and exec only the function
    src = pathlib.Path(__file__).parent.parent.joinpath("server.py").read_text()
    # Extract and exec parse_multipart
    import re
    # Find the function
    m = re.search(r"(def parse_multipart\(.*?)(?=\ndef )", src, re.DOTALL)
    assert m, "Could not find parse_multipart in server.py"
    ns = {}
    exec("import re as _re, email.parser as _ep\n" + m.group(1), ns)
    parse_multipart = ns["parse_multipart"]

    # Build a minimal multipart body
    boundary = b"testboundary"
    body = (
        b"--testboundary\r\n"
        b"Content-Disposition: form-data; name=\"session_id\"\r\n\r\n"
        b"abc123\r\n"
        b"--testboundary\r\n"
        b"Content-Disposition: form-data; name=\"file\"; filename=\"hello.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"hello world\r\n"
        b"--testboundary--\r\n"
    )
    fields, files = parse_multipart(
        io.BytesIO(body),
        "multipart/form-data; boundary=testboundary",
        len(body)
    )
    assert fields.get("session_id") == "abc123", f"fields: {fields}"
    assert "file" in files, f"files: {files}"
    filename, content = files["file"]
    assert filename == "hello.txt"
    assert content == b"hello world"


def test_parse_multipart_binary_file():
    """parse_multipart handles binary (PNG header bytes) without corruption."""
    src = pathlib.Path(__file__).parent.parent.joinpath("server.py").read_text()
    import re
    m = re.search(r"(def parse_multipart\(.*?)(?=\ndef )", src, re.DOTALL)
    ns = {}
    exec("import re as _re, email.parser as _ep\n" + m.group(1), ns)
    parse_multipart = ns["parse_multipart"]

    # Fake PNG: first 8 bytes of PNG magic
    png_magic = b"\x89PNG\r\n\x1a\n"
    boundary = b"binboundary"
    body = (
        b"--binboundary\r\n"
        b"Content-Disposition: form-data; name=\"session_id\"\r\n\r\n"
        b"sess1\r\n"
        b"--binboundary\r\n"
        b"Content-Disposition: form-data; name=\"file\"; filename=\"test.png\"\r\n"
        b"Content-Type: image/png\r\n\r\n" + png_magic + b"\r\n"
        b"--binboundary--\r\n"
    )
    fields, files = parse_multipart(
        io.BytesIO(body),
        "multipart/form-data; boundary=binboundary",
        len(body)
    )
    assert "file" in files
    filename, content = files["file"]
    assert filename == "test.png"
    assert content == png_magic, f"Binary content corrupted: {content!r}"


# ──────────────────────────────────────────────
# File upload via HTTP
# ──────────────────────────────────────────────

def test_upload_text_file(cleanup_test_sessions):
    """Upload a text file to a session workspace, verify it appears in /api/list."""
    sid, _ = make_session_tracked(cleanup_test_sessions)

    result, status = post_multipart("/api/upload", {"session_id": sid}, {
        "file": ("test_upload.txt", b"sprint1 test content")
    })
    assert status == 200, f"Upload failed {status}: {result}"
    assert "filename" in result
    assert result["size"] == len(b"sprint1 test content")

    # Verify file appears in listing
    listing = get(f"/api/list?session_id={sid}&path=.")
    names = [e["name"] for e in listing["entries"]]
    assert result["filename"] in names, f"{result['filename']} not in {names}"


def test_upload_too_large(cleanup_test_sessions):
    """Uploading a file over MAX_UPLOAD_BYTES is rejected (413 or connection closed)."""
    sid, _ = make_session_tracked(cleanup_test_sessions)

    # 21MB > 20MB limit
    big = b"x" * (21 * 1024 * 1024)
    try:
        result, status = post_multipart("/api/upload", {"session_id": sid}, {
            "file": ("big.bin", big)
        })
        # If we get a response it should be 413
        assert status == 413, f"Expected 413, got {status}: {result}"
    except (urllib.error.URLError, ConnectionResetError, BrokenPipeError):
        # Server closed connection after reading Content-Length > limit before body
        # This is also valid rejection behavior
        pass


def test_upload_no_file_field(cleanup_test_sessions):
    """Upload with no file field returns 400."""
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post_multipart("/api/upload", {"session_id": sid}, {})
    assert status == 400, f"Expected 400, got {status}: {result}"


def test_upload_bad_session():
    """Upload to nonexistent session returns 404."""
    result, status = post_multipart("/api/upload", {"session_id": "nosuchsession"}, {
        "file": ("x.txt", b"data")
    })
    assert status == 404, f"Expected 404, got {status}: {result}"


# ──────────────────────────────────────────────
# Approval API
# ──────────────────────────────────────────────

def test_approval_pending_none():
    """GET /api/approval/pending for a session with no pending entry returns null."""
    data = get("/api/approval/pending?session_id=no_such_session")
    assert data["pending"] is None


def test_approval_submit_and_respond():
    """Inject a pending approval via server endpoint, retrieve it, respond with deny."""
    test_sid = f"test-approval-{uuid.uuid4().hex[:6]}"
    cmd = "rm -rf /tmp/testdir"
    key = "recursive_delete"

    # Inject into server process via test endpoint (shared module state)
    inject = get(f"/api/approval/inject_test?session_id={urllib.parse.quote(test_sid)}&pattern_key={key}&command={urllib.parse.quote(cmd)}")
    assert inject["ok"] is True

    # Poll should now show the pending entry
    data = get(f"/api/approval/pending?session_id={urllib.parse.quote(test_sid)}")
    assert data["pending"] is not None, "Pending entry not visible after inject"
    assert data["pending"]["command"] == cmd

    # Respond with deny
    result, status = post("/api/approval/respond", {
        "session_id": test_sid,
        "choice": "deny"
    })
    assert status == 200
    assert result["ok"] is True
    assert result["choice"] == "deny"

    # Pending should be gone
    data2 = get(f"/api/approval/pending?session_id={urllib.parse.quote(test_sid)}")
    assert data2["pending"] is None, "Pending entry should be cleared after respond"


def test_approval_respond_allow_session():
    """Inject pending entry, respond with session choice, verify cleared (approved)."""
    test_sid = f"test-approval-sess-{uuid.uuid4().hex[:6]}"

    inject = get(f"/api/approval/inject_test?session_id={urllib.parse.quote(test_sid)}&pattern_key=force_kill&command=pkill+-9+someproc")
    assert inject["ok"] is True

    result, status = post("/api/approval/respond", {
        "session_id": test_sid,
        "choice": "session"
    })
    assert status == 200
    assert result["ok"] is True
    assert result["choice"] == "session"

    # After session approval, pending should be cleared
    data = get(f"/api/approval/pending?session_id={urllib.parse.quote(test_sid)}")
    assert data["pending"] is None, "Pending entry should be cleared after session approval"


# ──────────────────────────────────────────────
# Stream status endpoint (B4/B5)
# ──────────────────────────────────────────────

def test_stream_status_unknown_id():
    """GET /api/chat/stream/status for unknown stream_id returns active:false."""
    data = get("/api/chat/stream/status?stream_id=doesnotexist")
    assert data["active"] is False


# ──────────────────────────────────────────────
# File browser
# ──────────────────────────────────────────────

def test_list_dir(cleanup_test_sessions):
    """List workspace directory for a session."""
    sid, _ = make_session_tracked(cleanup_test_sessions)
    listing = get(f"/api/list?session_id={sid}&path=.")
    assert "entries" in listing
    assert isinstance(listing["entries"], list)


def test_list_dir_path_traversal(cleanup_test_sessions):
    """Path traversal via ../.. should be blocked (500 or 400)."""
    sid, _ = make_session_tracked(cleanup_test_sessions)
    try:
        listing = get(f"/api/list?session_id={sid}&path=../../etc")
        # If server returns entries outside workspace root, that is a bug
        # (safe_resolve should raise ValueError)
        assert False, f"Expected error for path traversal, got: {listing}"
    except urllib.error.HTTPError as e:
        assert e.code in (400, 404, 500), f"Expected 400/404/500 for traversal, got {e.code}"
