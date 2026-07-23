"""Regression tests for stale empty sessions after a WebUI restart.

When a saved session ID returns 404 (e.g. the session was deleted from another
browser, or a state DB rotation removed it), the prior behavior was to show
\"Session not available in web UI.\" and stick there forever — the saved
localStorage entry never got cleared, so every reload reproduced the broken
state.

These tests lock in:
  1. ``api()`` attaches HTTP context (``.status``, ``.statusText``, ``.body``)
     to thrown errors so callers can branch on status without re-parsing text.
  2. ``loadSession()`` clears the stale ``hermes-webui-session`` key on a 404
     and strips the ``/session/<id>`` URL, then rethrows only at boot time so
     boot can fall through to the empty state (#2798, #2782).
  3. The server 404s a deleted *WebUI* session on ``GET /api/session`` instead
     of synthesising a read-only CLI stub, so ``GET`` and the ``POST`` write
     paths agree on whether a session exists and the client can self-heal
     (#2782). A genuine CLI-origin session still returns 200 after its sidecar
     is gone.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
import re


REPO = Path(__file__).parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _api_body() -> str:
    m = re.search(r"async function api\(path,opts=.*?\n\}", WORKSPACE_JS, re.DOTALL)
    assert m, "api() function must exist in workspace.js"
    return m.group(0)


def _load_session_error_block() -> str:
    start = SESSIONS_JS.find("data = await api(`/api/session?")
    assert start > 0, "loadSession metadata request not found"
    catch_idx = SESSIONS_JS.find("} catch(e) {", start)
    assert catch_idx > start, "loadSession metadata catch block not found"
    # The catch opens with a stale-load guard that itself contains an early
    # `return;` (#3993 Codex race fix). Skip past that guard so we extract the
    # 404 / non-404 self-heal body, not just the guard.
    body_start = SESSIONS_JS.find("if(_msgInner){", catch_idx)
    assert body_start > catch_idx, "loadSession catch body not found"
    end = SESSIONS_JS.find("return;", body_start)
    assert end > body_start, "loadSession metadata catch return not found"
    return SESSIONS_JS[catch_idx:end]


def _load_session_404_block() -> str:
    """The body of the `if(e.status===404){ ... }` arm only."""
    block = _load_session_error_block()
    start = block.find("if(e.status===404){")
    assert start >= 0, "loadSession 404 arm not found"
    # The 404 arm is closed by the `} else {` of the outer status check.
    end = block.find("} else {", start)
    assert end > start, "loadSession 404 arm terminator not found"
    return block[start:end]


def _send_catch_block() -> str:
    """The catch(e) body of send() after POST /api/chat/start."""
    start = MESSAGES_JS.find("const startData=await api('/api/chat/start'")
    assert start > 0, "send() /api/chat/start call not found"
    catch_idx = MESSAGES_JS.find("}catch(e){", start)
    assert catch_idx > start, "send() catch block not found"
    # Stop at the conflictActiveStream marker; the 404 branch must precede it.
    end = MESSAGES_JS.find("const conflictActiveStream", catch_idx)
    assert end > catch_idx, "send() catch conflictActiveStream marker not found"
    return MESSAGES_JS[catch_idx:end]


def test_api_http_errors_preserve_response_status():
    """Callers must be able to distinguish stale-session 404s from generic failures."""
    body = _api_body()
    assert re.search(r"\w+\.status\s*=\s*res\.status", body), (
        "api() must attach res.status to thrown HTTP errors"
    )
    assert re.search(r"\w+\.statusText\s*=\s*res\.statusText", body), (
        "api() must attach res.statusText to thrown HTTP errors"
    )
    assert re.search(r"\w+\.body\s*=\s*text", body), (
        "api() must attach the raw error body to thrown HTTP errors"
    )


def test_load_session_clears_saved_stale_404_and_rethrows_to_boot():
    """A missing saved session should be removed and let boot show the empty state."""
    block = _load_session_error_block()
    assert "e.status===404" in block, "loadSession must keep a 404-specific branch"
    assert "localStorage.removeItem('hermes-webui-session')" in block, (
        "loadSession must clear stale saved session IDs on 404"
    )
    assert "history.replaceState" in block, (
        "loadSession must strip stale /session/{id} from the URL so a refresh "
        "doesn't re-trigger the 404 loop"
    )
    assert "_loadingSessionId = null" in block, (
        "loadSession must clear the in-flight load marker on 404"
    )
    # Boot-time (!currentSid) rethrow so boot falls through to the empty state.
    assert "!currentSid" in block, (
        "loadSession must keep the !currentSid gate around the boot-time rethrow"
    )
    assert re.search(r"throw\s+e", block), (
        "loadSession must rethrow the stale saved-session 404 so boot can fall "
        "through to the no-session empty state"
    )


def test_load_session_404_self_heal_gated_to_active_or_boot():
    """#2782: the localStorage clear + URL strip self-heal runs only when the
    404'd id is the one being activated, gated on (!currentSid || currentSid===sid):
    a boot-time restore (#2798) or a reload of the *current* session whose sidecar
    was deleted. A click into a *different* dead session preserves the live
    session's saved id and URL. Only the rethrow stays gated on !currentSid."""
    arm = _load_session_404_block()
    self_heal = "if(!currentSid || currentSid===sid)"
    assert self_heal in arm, (
        "self-heal must be gated to boot or the active session, not unconditional"
    )
    heal_idx = arm.find(self_heal)
    clear_idx = arm.find("localStorage.removeItem('hermes-webui-session')")
    strip_idx = arm.find("history.replaceState")
    assert clear_idx > heal_idx, "localStorage clear must run inside the self-heal gate"
    assert strip_idx > heal_idx, "URL strip must run inside the self-heal gate"
    # The boot-time rethrow stays nested on !currentSid, inside the self-heal gate.
    rethrow_gate_idx = arm.find("if(!currentSid)")
    assert rethrow_gate_idx > heal_idx, "the !currentSid rethrow gate must remain"
    assert re.search(r"throw\s+e", arm[rethrow_gate_idx:]), (
        "the !currentSid gate must still contain the boot-time rethrow"
    )


def test_send_chat_start_404_self_heals_instead_of_error_bubble():
    """#2782: POST /api/chat/start 404 (deleted sidecar) must clear localStorage,
    strip the URL, and reset to empty state, before the generic error path that
    would otherwise push an "Error:" bubble into the chat."""
    block = _send_catch_block()
    assert "e.status===404" in block, (
        "send() must branch on a 404 from /api/chat/start before the generic path"
    )
    assert "localStorage.removeItem('hermes-webui-session')" in block, (
        "send() 404 branch must clear the saved session key"
    )
    assert "history.replaceState" in block, (
        "send() 404 branch must strip the stale /session/<id> URL"
    )
    assert re.search(r"return\s*;", block), (
        "send() 404 branch must return before pushing an error bubble"
    )
    # The error bubble (`**Error:**`) lives after the conflictActiveStream
    # marker, so confirming the 404 branch + return precede that marker is
    # enough to prove no bubble is appended on a 404.
    assert "**Error:**" not in block, (
        "the 404 self-heal branch must run before the error-bubble path"
    )


# ── Server: GET /api/session 404s a deleted WebUI session (#2782) ──


def _invoke_api_session_keyerror(*, index_json, cli_messages):
    """Drive GET /api/session with get_session() raising KeyError (the deleted-
    session fallthrough) and a patched _index.json. Returns the captured status.
    """
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    def fake_bad(_handler, msg, status=400):
        captured["data"] = {"error": msg}
        captured["status"] = status
        return {"error": msg}

    class _FakeIndexFile:
        def exists(self):
            return index_json is not None

        def read_text(self, encoding="utf-8"):
            return index_json

        def read_bytes(self):
            # The index reader now parses raw bytes (json.loads decodes UTF-8 in
            # one pass); model that so this fake matches the real Path interface.
            return None if index_json is None else index_json.encode("utf-8")

    parsed = urlparse("/api/session?session_id=gone_001&messages=0&resolve_model=0")
    with patch("api.routes.get_session", side_effect=KeyError("gone_001")), \
         patch("api.routes.SESSION_INDEX_FILE", _FakeIndexFile()), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_cli_session_messages", return_value=cli_messages), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes.bad", side_effect=fake_bad):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured


def test_get_session_404s_deleted_webui_session():
    """A WebUI session in _index.json (no/webui source) whose sidecar is gone
    must 404 on GET, not synthesise a read-only CLI stub, so the client can
    self-heal and POST/GET agree (#2782)."""
    index = '[{"session_id": "gone_001", "source_tag": null, "raw_source": null, "session_source": null}]'
    captured = _invoke_api_session_keyerror(
        index_json=index,
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 404, (
        "a deleted WebUI session must return 404, not a 200 CLI stub"
    )


def test_get_session_404s_deleted_fork_session():
    """A forked WebUI session is stamped session_source='fork' (the /api/session/
    branch handler); its deleted sidecar must 404 too, not fall through to a 200
    CLI stub, since a fork is WebUI-origin and bricks identically (#2782)."""
    index = '[{"session_id": "gone_001", "source_tag": null, "raw_source": null, "session_source": "fork"}]'
    captured = _invoke_api_session_keyerror(
        index_json=index,
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 404, (
        "a deleted fork (WebUI-origin) session must return 404, not a 200 CLI stub"
    )


def test_get_session_keeps_200_for_genuine_cli_session():
    """A genuine CLI-origin session (source_tag set to a non-webui value in the
    index) still returns the 200 CLI stub after its sidecar is gone (#2782)."""
    index = '[{"session_id": "gone_001", "source_tag": "claude-code", "raw_source": "claude-code", "session_source": "cli"}]'
    captured = _invoke_api_session_keyerror(
        index_json=index,
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 200, (
        "a genuine CLI session must keep the 200 CLI-stub path"
    )
    assert captured["data"]["session"]["session_id"] == "gone_001"


def test_get_session_keeps_200_when_id_absent_from_index():
    """An id absent from _index.json was never a WebUI session, so the existing
    CLI-store 200 path is preserved (no false-positive 404)."""
    captured = _invoke_api_session_keyerror(
        index_json='[{"session_id": "other_999", "source_tag": null}]',
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 200, (
        "an id not in the index must keep the CLI-store 200 path"
    )


def test_get_session_keeps_200_for_legacy_cli_row_with_blank_source():
    """Regression (#3501 review, Codex CORE catch): a legacy CLI/imported session
    can be present in _index.json with is_cli_session:true but BLANK source fields
    (source_tag/raw_source/session_source all null). The earlier
    `source_tag or raw_source or session_source or ""` collapse defaulted blank to
    WebUI and would WRONGLY 404 it. Per-field classification must treat a blank-
    source row marked is_cli_session (or read_only) as a genuine CLI session and
    keep the 200 stub."""
    index = (
        '[{"session_id": "gone_001", "source_tag": null, "raw_source": null, '
        '"session_source": null, "is_cli_session": true}]'
    )
    captured = _invoke_api_session_keyerror(
        index_json=index,
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 200, (
        "a legacy CLI row (is_cli_session:true, blank source) must keep the 200 "
        "CLI-stub path, not be 404'd as a deleted WebUI session"
    )


def test_get_session_keeps_200_for_read_only_row_with_blank_source():
    """A read-only imported session with blank source fields is also CLI-origin
    and must keep the 200 stub (companion to the is_cli_session case)."""
    index = (
        '[{"session_id": "gone_001", "source_tag": null, "raw_source": null, '
        '"session_source": null, "read_only": true}]'
    )
    captured = _invoke_api_session_keyerror(
        index_json=index,
        cli_messages=[{"role": "user", "content": "hi", "timestamp": 1}],
    )
    assert captured["status"] == 200, (
        "a read-only imported row (blank source) must keep the 200 CLI-stub path"
    )
