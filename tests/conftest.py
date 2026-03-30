"""
Shared pytest fixtures for webui-mvp tests.

TEST ISOLATION:
  Tests run against a SEPARATE server instance on port 8788
  with a completely separate state directory at ~/.hermes/webui-mvp-test/.
  
  Your production server (port 8787) and real conversations are NEVER touched.
  The test state dir is wiped clean before each full test run.

  The test server is started automatically in the session-scoped `test_server`
  fixture and killed when the test session ends.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
import pytest

# ── Configuration ──────────────────────────────────────────────────────────
TEST_PORT      = 8788
TEST_BASE      = f"http://127.0.0.1:{TEST_PORT}"
TEST_STATE_DIR = pathlib.Path.home() / ".hermes" / "webui-mvp-test"
TEST_WORKSPACE = TEST_STATE_DIR / "test-workspace"
SERVER_SCRIPT  = pathlib.Path.home() / "webui-mvp" / "server.py"
VENV_PYTHON    = pathlib.Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
HERMES_AGENT   = pathlib.Path.home() / ".hermes" / "hermes-agent"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _post(base, path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {}


def _wait_for_server(base, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    return True
        except Exception:
            time.sleep(0.3)
    return False


# ── Session-scoped test server ───────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def test_server():
    """
    Start an isolated test server on port 8788 pointing at the test state dir.
    Wipe the test state dir before starting so each full run is clean.
    Kill the server when all tests finish.
    """
    # Wipe and recreate test state dir for a clean slate
    if TEST_STATE_DIR.exists():
        shutil.rmtree(TEST_STATE_DIR)
    TEST_STATE_DIR.mkdir(parents=True)
    TEST_WORKSPACE.mkdir(parents=True)

    env = os.environ.copy()
    env.update({
        "HERMES_WEBUI_PORT":              str(TEST_PORT),
        "HERMES_WEBUI_HOST":              "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR":         str(TEST_STATE_DIR),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(TEST_WORKSPACE),
        "HERMES_WEBUI_DEFAULT_MODEL":     "openai/gpt-5.4-mini",
    })

    proc = subprocess.Popen(
        [str(VENV_PYTHON), str(SERVER_SCRIPT)],
        cwd=str(HERMES_AGENT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_for_server(TEST_BASE):
        proc.kill()
        pytest.fail(f"Test server on port {TEST_PORT} did not start within 15 seconds.")

    yield proc  # tests run here

    # Teardown: kill the test server
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # Optionally wipe the test state dir after tests complete
    try:
        shutil.rmtree(TEST_STATE_DIR)
    except Exception:
        pass


# ── Test base URL override ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def base_url():
    """Returns the base URL of the isolated test server."""
    return TEST_BASE


# ── Per-test session tracking and cleanup ───────────────────────────────────

@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """
    Yields a list for tests to register created session IDs.
    After each test, deletes all registered sessions.
    Falls back to cleanup_zero_message for anything missed.
    """
    created: list[str] = []
    yield created
    for sid in created:
        try:
            _post(TEST_BASE, "/api/session/delete", {"session_id": sid})
        except Exception:
            pass
    # Belt-and-suspenders: also wipe all 0-message sessions from the test dir
    try:
        _post(TEST_BASE, "/api/sessions/cleanup_zero_message")
    except Exception:
        pass


# ── Convenience helpers exported for use in test files ──────────────────────

def make_session_tracked(created_list, ws=None):
    """
    Create a session on the TEST server and register it for cleanup.
    Use instead of calling /api/session/new directly.

    Usage:
        def test_something(cleanup_test_sessions):
            sid, ws = make_session_tracked(cleanup_test_sessions)
    """
    body = {}
    if ws:
        body["workspace"] = str(ws)
    d = _post(TEST_BASE, "/api/session/new", body)
    sid = d["session"]["session_id"]
    ws_path = pathlib.Path(d["session"]["workspace"])
    created_list.append(sid)
    return sid, ws_path
