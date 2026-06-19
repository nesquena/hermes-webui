"""Regression tests for #4490 pre-session toolset staging."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from api.models import new_session
from api.routes import handle_post

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


class _DummyHandler:
    command = "POST"

    def __init__(self, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = tempfile.SpooledTemporaryFile()
        self.rfile.write(raw)
        self.rfile.seek(0)
        self.status = None
        self.response = {}
        self.wfile = tempfile.SpooledTemporaryFile()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, code: int):
        self.status = code

    def send_header(self, key: str, value: str):
        self.response.setdefault("headers", {})[key] = value

    def end_headers(self):
        pass

    def payload(self) -> dict:
        self.wfile.seek(0)
        return json.loads(self.wfile.read().decode("utf-8"))


def test_toggle_toolsets_dropdown_opens_without_session_guard():
    toggle = _function_body(UI_JS, "function toggleToolsetsDropdown")

    assert "!S.session" not in toggle
    assert "chip.offsetParent === null" in toggle
    assert "_populateToolsetsDropdown();" in toggle


def test_apply_toolsets_stages_without_session_and_skips_post():
    apply_body = _function_body(UI_JS, "function _applySessionToolsets")
    compact = apply_body.replace(" ", "")

    assert "S._pendingSessionToolsets=toolsets" in compact
    pre_session_branch = apply_body[: apply_body.index("const sid = S.session.session_id;")]
    assert "api('/api/session/toolsets'" not in pre_session_branch
    assert "_applyToolsetsChip(toolsets)" in pre_session_branch


def test_staged_toolsets_are_visibly_marked_on_chip():
    apply_chip = _function_body(UI_JS, "function _applyToolsetsChip")

    assert "(staged)" in apply_chip
    assert "!S.session" in apply_chip
    assert "S._pendingSessionToolsets" in apply_chip


def test_new_session_request_consumes_pending_toolsets_once():
    compact = SESSIONS_JS.replace(" ", "")

    post_start = compact.index("api('/api/session/new'")
    before_post = compact[:post_start]
    after_assignment = compact[compact.index("S.session=data.session") :]

    assert "Array.isArray(S._pendingSessionToolsets)" in before_post
    assert "reqBody.enabled_toolsets=S._pendingSessionToolsets" in before_post
    assert "S._pendingSessionToolsets=null" in after_assignment


def test_pending_toolsets_only_forwarded_from_empty_composer():
    """The staged override must only be forwarded when there is no active session.

    Without the `!S.session` guard, a toolset staged on the empty composer would
    leak into a later New Chat started from an existing session (#4490 follow-up).
    """
    compact = SESSIONS_JS.replace(" ", "")
    post_start = compact.index("api('/api/session/new'")
    before_post = compact[:post_start]
    # The forwarding line must be gated on the no-session (empty composer) state.
    assert "!S.session&&Array.isArray(S._pendingSessionToolsets)" in before_post


def test_load_existing_session_clears_staged_toolsets():
    """Loading a real existing session must clear any abandoned staged override.

    loadSession() assigns S.session=data.session on the success path; the staged
    value is cleared there so a subsequent New Chat does not inherit it (#4490).
    """
    body = _function_body(SESSIONS_JS, "async function loadSession")
    compact = body.replace(" ", "")
    assign = compact.index("S.session=data.session")
    # The clear must accompany the real-session assignment, not only the create path.
    assert "S._pendingSessionToolsets=null" in compact[assign : assign + 400]


def test_workspace_and_profile_switches_clear_pending_toolsets():
    for marker in (
        "function promptWorkspacePath",
        "function switchToWorkspace",
        "function switchToProfile",
    ):
        body = _function_body(PANELS_JS, marker)
        assert "S._pendingSessionToolsets=null" in body.replace(" ", "")

    assert "S._pendingSessionToolsets=null" in UI_JS.replace(" ", "")


def test_context_switch_auto_sessions_do_not_forward_staged_toolsets():
    """Workspace/profile context switches intentionally reset staged values.

    The staged toolset belongs to the empty composer context. Auto-creating a
    workspace/file session from another context should not silently carry it
    forward; those paths clear the staged value after creating their session.
    """
    for src, marker in (
        (PANELS_JS, "function promptWorkspacePath"),
        (PANELS_JS, "function switchToWorkspace"),
        (UI_JS, "function promptNewFile"),
        (UI_JS, "function promptNewFolder"),
    ):
        body = _function_body(src, marker)
        assert "enabled_toolsets" not in body
        assert "S._pendingSessionToolsets=null" in body.replace(" ", "")


def test_backend_new_session_accepts_enabled_toolsets():
    with tempfile.TemporaryDirectory() as tmp:
        session = new_session(workspace=tmp, enabled_toolsets=["filesystem", "shell"])

    assert session.enabled_toolsets == ["filesystem", "shell"]


def test_api_session_new_accepts_enabled_toolsets():
    with tempfile.TemporaryDirectory() as tmp, patch(
        "api.routes.get_last_workspace", return_value=tmp
    ):
        handler = _DummyHandler({"enabled_toolsets": ["filesystem", "shell"]})
        handle_post(handler, urlparse("/api/session/new"))

    payload = handler.payload()
    assert handler.status == 200
    assert payload["session"]["enabled_toolsets"] == ["filesystem", "shell"]


def test_api_session_new_rejects_malformed_enabled_toolsets():
    cases = [
        {"enabled_toolsets": []},
        {"enabled_toolsets": "filesystem"},
        {"enabled_toolsets": ["filesystem", ""]},
        {"enabled_toolsets": ["filesystem", 42]},
    ]
    with tempfile.TemporaryDirectory() as tmp, patch(
        "api.routes.get_last_workspace", return_value=tmp
    ):
        for body in cases:
            handler = _DummyHandler(body)
            handle_post(handler, urlparse("/api/session/new"))
            assert handler.status == 400
