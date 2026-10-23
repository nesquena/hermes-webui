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
* the client restores the typed message + attachments and asks for a manual refresh (no auto-reload).
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
STALE_HEADER = "X-Hermes-WebUI-Bundle"


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
    """curl, pytest handlers and scripts (no Origin/Referer) must not be locked out."""
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    h = _Handler({})
    assert routes._stale_client_bundle_response(h) is None


def test_missing_header_is_never_rejected_even_from_a_page(routes, monkeypatch):
    """Clients that do not send the header at all — WebUI Bridge API clients,
    older native shells, any browser page that predates the stamp — must keep
    working. Origin/Referer are not a substitute for the version header."""
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    for hdrs in (
        {"Origin": "https://host.example"},
        {"Referer": "https://host.example/session/abc"},
        {"Origin": "https://host.example", STALE_HEADER: ""},
        {STALE_HEADER: "   "},
    ):
        assert routes._stale_client_bundle_response(_Handler(hdrs)) is None, hdrs


def test_non_string_header_object_is_ignored(routes, monkeypatch):
    """Test doubles hand the barrier a Mock() handler whose headers.get()
    returns a Mock; that must read as 'no header', not leak into a JSON body."""
    from unittest.mock import Mock
    monkeypatch.setattr(routes, "WEBUI_VERSION", "exp-v9.9.9", raising=False)
    assert routes._stale_client_bundle_response(Mock()) is None


def test_url_quoted_version_stamp_is_accepted(routes, monkeypatch):
    """The page is stamped with quote(WEBUI_VERSION, safe="") (see
    _render_index_shell_base). A build version with "+" (setuptools-scm local
    segment) therefore arrives as "%2B"; a fresh page must not be rejected by
    its own encoding — that would reload once and then brick every send."""
    from urllib.parse import quote
    version = "v0.50.124.dev1+gabc123"
    monkeypatch.setattr(routes, "WEBUI_VERSION", version, raising=False)
    assert quote(version, safe="") != version  # the case under test
    for sent in (version, quote(version, safe="")):
        assert routes._stale_client_bundle_response(_Handler({STALE_HEADER: sent})) is None, sent
    # and a genuinely different version is still caught, in either spelling
    for sent in ("v0.50.123+gdef456", quote("v0.50.123+gdef456", safe="")):
        payload = routes._stale_client_bundle_response(_Handler({STALE_HEADER: sent}))
        assert payload is not None and payload["type"] == "stale_client_bundle", sent


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


def test_cors_preflight_allows_the_bundle_header(routes):
    """api() sends the header on every call, so an allowlisted cross-origin page
    must see it in Access-Control-Allow-Headers or the browser refuses every
    request at preflight."""
    h = _Handler({"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1:8787"})
    routes.apply_cors_preflight_headers(h)
    allowed = {x.strip().lower() for x in h._sent_headers["Access-Control-Allow-Headers"].split(",")}
    assert STALE_HEADER.lower() in allowed
    assert routes.STALE_CLIENT_BUNDLE_HEADER == STALE_HEADER
    assert {"content-type", "authorization"} <= allowed


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


def test_client_restores_draft_and_asks_for_refresh_on_stale_bundle():
    """The /api/chat/start error path recognises the typed response, puts the
    user's draft back through the shared failed-send recovery, and asks for a
    manual refresh. It must NOT reload the page itself: a reload from inside a
    half-finished send races the draft clear/save, queued follow-ups, newer
    typing and staged File objects (#8156 review)."""
    anchor = "_isStaleClientBundleError(e)){"
    assert anchor in MESSAGES_JS, "chat-start catch must branch on the typed stale-bundle error"
    i = MESSAGES_JS.index(anchor)
    window = MESSAGES_JS[i:]
    window = window[:window.index("if(e&&e.status===404)")]  # this branch only
    assert "location.reload(" not in window
    assert "sessionStorage" not in window
    # Remove only this send's turn — optimisticMessages is a copy of the whole
    # transcript, filtering it out would blank the conversation.
    assert "S.messages.filter(m=>m!==userMsg)" in window
    assert "optimisticMessages.includes" not in window
    # Reuse the shared failed-send recovery (text + files + owning session +
    # ordering after the send-time clear) instead of a hand-rolled restore.
    assert "_restoreComposerDraftAfterFailedSend(_failedSendDraftText, _failedSendFilesSnapshot, activeSid, _composerDraftClearPromise, _goalContinuationId)" in window
    # The rejected turn's pollers and cards are torn down like any other
    # pre-start rejection.
    assert "stopClarifyPollingForSession(activeSid)" in window
    assert "hideClarifyCard(true, 'terminal')" in window
    assert "hideApprovalCard(true)" in window
    # The user is told what to do (behaviour: tests/test_8156_stale_bundle_recovery.py).
    assert "'stale_bundle_refresh'" in window
    assert "'stale_bundle_refresh_files'" in window
    assert "sessionStorage.removeItem('hermes-webui-stale-bundle-reload')" not in MESSAGES_JS


def test_stale_bundle_message_exists_in_every_locale():
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    locale_blocks = i18n.count("regen_failed:")
    assert locale_blocks >= 15
    assert i18n.count("stale_bundle_refresh:") == locale_blocks
    assert "stale_bundle_reloading" not in i18n
    assert "stale_bundle_reload_failed" not in i18n
