"""Stale client bundle barrier (#8155).

A native shell (Hermex iOS) or a long-lived tab can keep running a frontend
bundle loaded from a previous WebUI process indefinitely: it never re-navigates,
so neither the ``?v=`` cache-bust nor the service worker ever fires. The old
bundle then talks to a newer server with mismatched contracts (in #8155 the old
client relied on the removed ``/api/approval/stream`` SSE path, so approval cards
never appeared and the agent waited out the 300 s timeout).

Contract:

* the client sends its bundle version on every ``api()`` call as the
  ``X-Hermes-WebUI-Bundle`` header (value of ``window.__HERMES_WEBUI_BUNDLE_VERSION__``);
* ``/api/chat/start`` rejects a mismatched bundle with ``409`` and
  ``{"type": "stale_client_bundle", ...}`` BEFORE touching any session state,
  mirroring the existing stale-agent-runtime barrier;
* a missing header is accepted (curl, tests, older clients that predate the header);
* the client reloads once on that response, preserving the typed message.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


# ── server ────────────────────────────────────────────────────────────────────


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in: captures j()/bad() output."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.status = None
        self._sent_headers: dict[str, str] = {}
        self.client_address = ("127.0.0.1", 0)
        self.path = "/api/chat/start"
        self.command = "POST"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self._sent_headers[k] = v

    def end_headers(self):
        pass

    def body_json(self):
        raw = self.wfile.getvalue()
        return json.loads(raw.decode("utf-8")) if raw else None


@pytest.fixture
def routes():
    import api.routes as _routes
    return _routes


def test_barrier_helper_exists(routes):
    assert hasattr(routes, "_stale_client_bundle_response"), (
        "api.routes must expose _stale_client_bundle_response(handler) -> dict | None"
    )


def test_mismatched_bundle_is_rejected(routes, monkeypatch):
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    h = _Handler({"X-Hermes-WebUI-Bundle": "exp-v1.0.0"})
    payload = routes._stale_client_bundle_response(h)
    assert payload is not None
    assert payload["type"] == "stale_client_bundle"
    assert payload["server_version"] == "exp-v9.9.9"
    assert payload["client_version"] == "exp-v1.0.0"
    assert payload.get("retryable") is True


def test_matching_bundle_passes(routes, monkeypatch):
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    h = _Handler({"X-Hermes-WebUI-Bundle": "exp-v9.9.9"})
    assert routes._stale_client_bundle_response(h) is None


def test_missing_header_passes(routes, monkeypatch):
    """curl, pytest handlers and pre-header clients must not be locked out."""
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    h = _Handler({})
    assert routes._stale_client_bundle_response(h) is None


def test_chat_start_returns_409_before_touching_session(routes, monkeypatch):
    """The barrier must run before _get_or_materialize_session, like the
    stale-runtime barrier: a stale client must not claim/mutate a session."""
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)

    touched = []

    def _boom(*a, **k):
        touched.append(a)
        raise AssertionError("session must not be materialised for a stale client")

    monkeypatch.setattr(routes, "_get_or_materialize_session", _boom)
    h = _Handler({"X-Hermes-WebUI-Bundle": "exp-v1.0.0"})
    routes._handle_chat_start(h, {"session_id": "abc123", "message": "hi"})
    assert h.status == 409
    body = h.body_json()
    assert body["type"] == "stale_client_bundle"
    assert touched == []


def test_chat_start_source_order_barrier_precedes_session_lookup():
    """Belt-and-braces on the source: the stale-client check sits before the
    session materialisation in _handle_chat_start."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.index("def _handle_chat_start(")
    body = src[start:start + 6000]
    i_barrier = body.index("_stale_client_bundle_response(")
    i_session = body.index("_get_or_materialize_session(")
    assert i_barrier < i_session


# ── client ────────────────────────────────────────────────────────────────────


def test_api_helper_sends_bundle_header():
    """Every api() call carries the running bundle's version."""
    m = re.search(r"async function api\(path,opts=\{\}\)\{(.*?)\n\}", WORKSPACE_JS, re.S)
    assert m, "api() helper not found in workspace.js"
    body = m.group(1)
    assert "X-Hermes-WebUI-Bundle" in body
    assert "__HERMES_WEBUI_BUNDLE_VERSION__" in body


def test_client_reloads_once_on_stale_bundle():
    """The /api/chat/start error path recognises the typed response, keeps the
    draft, and reloads — guarded so a persistent mismatch cannot loop."""
    anchor = "_isStaleClientBundleError(e)){"
    assert anchor in MESSAGES_JS, "chat-start catch must branch on the typed stale-bundle error"
    i = MESSAGES_JS.index(anchor)
    window = MESSAGES_JS[i:i + 1800]
    assert "location.reload(" in window
    # loop guard: a marker in sessionStorage so a second consecutive stale
    # response does not reload again
    assert "sessionStorage" in window
    assert "hermes-webui-stale-bundle-reload" in window
