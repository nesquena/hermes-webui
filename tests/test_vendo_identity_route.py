"""GET /api/vendo/identity returns the standard identity body when the
deployment runs behind Vendo SSO. Returns 404 otherwise.

These tests spin up isolated server subprocesses (per test) because
VENDO_AUTH is read from os.environ at call time inside the server process,
so the session-scoped conftest server cannot be used for env-varying tests.
Pattern mirrors test_tls_support.py.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
SERVER_SCRIPT = ROOT / "server.py"
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")


HEADERS_OK = {
    "X-Vendo-User-Id": "u_abc",
    "X-Vendo-User-Email": "yousef@vendo.run",
    "X-Vendo-User-Name": "Yousef Habib",
    "X-Vendo-Tenant-Id": "t_xyz",
    "X-Vendo-Role": "owner",
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection(host, port, timeout=2)
            c.request("GET", "/health")
            resp = c.getresponse()
            resp.read()
            c.close()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _start_server(port: int, extra_env: dict) -> tuple:
    """Start server.py as a subprocess with custom env. Returns (proc, state_dir)."""
    state_dir = tempfile.mkdtemp(prefix="vendo-sso-test-")
    workspace_dir = os.path.join(state_dir, "workspace")
    os.makedirs(workspace_dir, exist_ok=True)

    env = os.environ.copy()
    # Strip real provider keys
    for k in list(env):
        if any(k.startswith(p) for p in (
            "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
        )):
            del env[k]

    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_WEBUI_DEFAULT_WORKSPACE": workspace_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_DEFAULT_MODEL": "openai/gpt-5.4-mini",
    })
    # Remove keys that must be absent unless overridden
    env.pop("VENDO_AUTH", None)
    env.pop("HERMES_WEBUI_PASSWORD", None)
    env.update(extra_env)

    proc = subprocess.Popen(
        [VENV_PYTHON, str(SERVER_SCRIPT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, state_dir


def _http_get(port: int, path: str, headers: dict) -> tuple:
    """Make a GET to 127.0.0.1:port/path with the given headers.
    Returns (status_code, parsed_body_or_None).
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        data = r.read()
        try:
            body = json.loads(data) if data else None
        except ValueError:
            body = data
        return r.status, body
    finally:
        conn.close()


class _ServerFixture:
    """Context manager: starts a fresh server, tears it down on exit."""

    def __init__(self, extra_env: dict):
        self._extra_env = extra_env
        self._port = _find_free_port()
        self._proc = None
        self._state_dir = None

    def __enter__(self):
        self._proc, self._state_dir = _start_server(self._port, self._extra_env)
        if not _wait_for_server("127.0.0.1", self._port):
            self._proc.kill()
            raise RuntimeError(
                f"Test server on port {self._port} did not start in time."
            )
        return self

    def get(self, path: str, headers: dict) -> tuple:
        return _http_get(self._port, path, headers)

    def __exit__(self, *_):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._state_dir:
            shutil.rmtree(self._state_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

IDENTITY_PATH = "/api/vendo/identity"


def test_returns_identity_when_sso_on_and_headers_present():
    """When VENDO_AUTH=1 and X-Vendo-* headers are present, the endpoint
    returns the documented identity body.
    """
    with _ServerFixture({"VENDO_AUTH": "1"}) as srv:
        status, body = srv.get(IDENTITY_PATH, HEADERS_OK)
    assert status == 200
    assert body == {
        "user_id": "u_abc",
        "email": "yousef@vendo.run",
        "name": "Yousef Habib",
        "tenant_id": "t_xyz",
        "role": "owner",
        "logout_url": "/__vendo/auth/logout",
        "dashboard_url": "https://vendo.run/dashboard",
    }


def test_returns_404_when_sso_off():
    """When VENDO_AUTH is unset, the endpoint is hidden (404).
    Auth falls through (no password set), but the route sees is_enabled()==False
    and returns 404.
    """
    with _ServerFixture({}) as srv:
        status, _ = srv.get(IDENTITY_PATH, HEADERS_OK)
    assert status == 404


def test_returns_403_when_sso_on_but_headers_missing():
    """When VENDO_AUTH=1 but X-Vendo-* headers are absent, the auth
    short-circuit returns 403 before the route runs.
    """
    with _ServerFixture({"VENDO_AUTH": "1"}) as srv:
        status, _ = srv.get(IDENTITY_PATH, {})
    assert status == 403
