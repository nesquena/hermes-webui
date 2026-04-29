"""Contract tests for Task 05b onboarding `/api/setup/*` and `/` redirect behaviour.

These tests import ``Handler`` from Hermes WebUI ``server.py`` (repository root).
They are expected to fail until Task 05b implements the routes and middleware.

Run: ``cd forks/hermes-webui && pytest tests/test_setup_api.py -v``
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Package root: forks/hermes-webui/ (parent of tests/)
_WEBUI_ROOT = Path(__file__).resolve().parents[1]
if str(_WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEBUI_ROOT))


def _import_handler():
    """Return the WebUI HTTP handler class; fail the run if the checkout is missing."""
    try:
        from server import Handler  # type: ignore[import-not-found]
    except ImportError as exc:
        pytest.fail(
            "Could not import Handler from hermes-webui/server.py: "
            f"{exc!s}\n"
            "Initialize the hermes-webui submodule (Task 05b adds the behaviour these tests assert).",
        )
    return Handler


@pytest.fixture(scope="module")
def Handler_cls():
    """Hermes WebUI ``BaseHTTPRequestHandler`` subclass under test."""
    return _import_handler()


def _patch_auth_if_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass auth for local requests if ``check_auth`` exists on ``server``."""
    try:
        import server as server_module  # type: ignore[import-not-found]
    except ImportError:
        return
    if hasattr(server_module, "check_auth"):
        monkeypatch.setattr(server_module, "check_auth", lambda *a, **kw: True)


class LogCapture(logging.Handler):
    """Accumulates log records from any thread (suited to threaded ``HTTPServer``)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


@pytest.fixture
def log_capture() -> LogCapture:
    """Attach a root handler so server-thread logging is visible to assertions."""
    capture = LogCapture()
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.addHandler(capture)
    root_logger.setLevel(logging.DEBUG)
    yield capture
    root_logger.removeHandler(capture)
    root_logger.setLevel(prev_level)


class _ServerHarness:
    """Minimal threaded ``HTTPServer`` for exercising ``Handler``."""

    def __init__(self, handler_cls: type) -> None:
        self._server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class HttpTestClient:
    """Thin client over ``http.client`` (stdlib only)."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection(self._host, self._port)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            hdrs = {k: v for k, v in resp.getheaders()}
            return resp.status, hdrs, raw
        finally:
            conn.close()

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return self.request("GET", path)

    def post_json(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
        return self.request("POST", path, body=data, headers=headers)


@pytest.fixture
def client(Handler_cls, monkeypatch):
    """Live ``HTTPServer`` bound to the WebUI handler.

    Environment overrides (e.g. ``ONBOARDING_PATH``) must be applied in fixtures
    that run *before* this one — not in the test body — if the handler reads
    config at import or server start time.
    """
    _patch_auth_if_present(monkeypatch)
    harness = _ServerHarness(Handler_cls)
    harness.start()
    try:
        yield HttpTestClient(harness.host, harness.port)
    finally:
        harness.shutdown()


@pytest.fixture
def onboarding_incomplete(tmp_path, monkeypatch):
    """On disk onboarding state: not completed."""
    cfg = tmp_path / "onboarding.json"
    cfg.write_text('{"completed": false}', encoding="utf-8")
    monkeypatch.setenv("ONBOARDING_PATH", str(cfg))
    return cfg


def _json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _location(headers: dict[str, str]) -> str | None:
    for key, val in headers.items():
        if key.lower() == "location":
            return val
    return None


# --- Redirect middleware (4) ---


@pytest.fixture
def onboarding_path_missing(tmp_path, monkeypatch):
    """``ONBOARDING_PATH`` points at a path with no file (parent exists)."""
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("ONBOARDING_PATH", str(missing))


def test_redirect_when_onboarding_json_missing(client, onboarding_path_missing):
    """GET / with no onboarding file at ONBOARDING_PATH must redirect to /setup (302)."""
    status, headers, _ = client.get("/")
    assert status == 302
    loc = _location(headers)
    assert loc is not None
    assert loc.rstrip("/").endswith("/setup")


def test_redirect_when_completed_false(client, onboarding_incomplete):
    """GET / with ``{"completed": false}`` must redirect to /setup (302)."""
    status, headers, _ = client.get("/")
    assert status == 302
    loc = _location(headers)
    assert loc is not None
    assert loc.rstrip("/").endswith("/setup")


@pytest.fixture
def onboarding_completed(tmp_path, monkeypatch):
    """On disk onboarding state: completed."""
    cfg = tmp_path / "onboarding.json"
    cfg.write_text('{"completed": true}', encoding="utf-8")
    monkeypatch.setenv("ONBOARDING_PATH", str(cfg))


def test_no_redirect_when_completed_true(client, onboarding_completed):
    """GET / with ``{"completed": true}`` must not redirect (200 pass-through)."""
    status, _, _ = client.get("/")
    assert status == 200


def test_setup_and_api_routes_exempt_from_redirect(client, onboarding_incomplete):
    """GET /setup and POST /api/setup/openrouter must not be caught by the redirect (not 302)."""
    s1, _, _ = client.get("/setup")
    assert s1 != 302
    s2, _, _ = client.post_json("/api/setup/openrouter", {"key": ""})
    assert s2 != 302


# --- OpenRouter key endpoint (4) ---


def test_openrouter_valid_key_returns_ok(client, log_capture):
    """Valid ``sk-`` prefixed key must return 200 and ``{"ok": true}``."""
    status, _, raw = client.post_json("/api/setup/openrouter", {"key": "sk-test-key"})
    log_text = "\n".join(log_capture.records)
    assert "sk-test-key" not in log_text
    assert status == 200
    body = _json_body(raw)
    assert body.get("ok") is True


@pytest.fixture
def hermes_env_at_tmp(tmp_path, monkeypatch):
    """Redirect ``hermes.env`` writes to a temp file (implementation reads ``HERMES_ENV_PATH``)."""
    env_path = tmp_path / "hermes.env"
    monkeypatch.setenv("HERMES_ENV_PATH", str(env_path))
    return env_path


def test_openrouter_valid_key_writes_env_file(client, hermes_env_at_tmp, log_capture):
    """After a valid POST, ``hermes.env`` must contain OPENROUTER_API_KEY=…."""
    env_path = hermes_env_at_tmp
    status, _, _ = client.post_json("/api/setup/openrouter", {"key": "sk-test-key"})
    log_text = "\n".join(log_capture.records)
    assert "sk-test-key" not in log_text
    assert status == 200
    content = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-test-key" in content


def test_openrouter_invalid_key_no_sk_prefix(client, log_capture):
    """A key without ``sk-`` prefix must yield 400, ok false, and an error string."""
    status, _, raw = client.post_json("/api/setup/openrouter", {"key": "notvalid"})
    log_text = "\n".join(log_capture.records)
    assert "notvalid" not in log_text
    assert status == 400
    body = _json_body(raw)
    assert body.get("ok") is False
    assert isinstance(body.get("error"), str) and body.get("error")


def test_openrouter_empty_key(client):
    """Empty key must yield 400, ok false, and an error string."""
    status, _, raw = client.post_json("/api/setup/openrouter", {"key": ""})
    assert status == 400
    body = _json_body(raw)
    assert body.get("ok") is False
    assert isinstance(body.get("error"), str) and body.get("error")


# --- Tailscale endpoints (4) ---


def test_tailscale_status_initial_is_waiting(client):
    """GET /api/setup/tailscale/status before any start must report status ``waiting``."""
    status, _, raw = client.get("/api/setup/tailscale/status")
    assert status == 200
    body = _json_body(raw)
    assert body.get("status") == "waiting"


@patch("subprocess.Popen")
def test_tailscale_start_returns_ok(mock_popen, client):
    """POST /api/setup/tailscale/start must return ok and spawn tailscale via Popen (mocked)."""
    mock_popen.return_value.stdout = iter([])
    mock_popen.return_value.poll = MagicMock(return_value=None)
    status, _, raw = client.post_json("/api/setup/tailscale/start", {})
    assert status == 200
    body = _json_body(raw)
    assert body.get("ok") is True
    mock_popen.assert_called_once()


@patch("subprocess.Popen")
def test_tailscale_start_idempotent(mock_popen, client):
    """A second start while tailscale is already running must not spawn another process."""
    proc = MagicMock()
    proc.stdout = iter([])
    proc.poll = MagicMock(return_value=None)
    mock_popen.return_value = proc
    r1, _, _ = client.post_json("/api/setup/tailscale/start", {})
    r2, _, _ = client.post_json("/api/setup/tailscale/start", {})
    assert r1 == 200 and r2 == 200
    mock_popen.assert_called_once()


@pytest.fixture
def onboarding_for_complete(tmp_path, monkeypatch):
    """Existing incomplete onboarding file for the complete endpoint."""
    cfg = tmp_path / "onboarding.json"
    cfg.write_text('{"completed": false}', encoding="utf-8")
    monkeypatch.setenv("ONBOARDING_PATH", str(cfg))
    return cfg


def test_tailscale_complete_writes_json(client, onboarding_for_complete):
    """POST /api/setup/complete must persist onboarding.json with completed and tailscale flags."""
    cfg = onboarding_for_complete
    status, _, _ = client.post_json(
        "/api/setup/complete",
        {"tailscale_connected": True},
    )
    assert status == 200
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data.get("completed") is True
    assert data.get("tailscale_connected") is True
