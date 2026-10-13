"""Regression tests for stale empty sessions after a WebUI restart.

When a route names a missing session while localStorage still names a valid
one, boot must remove only the stale route. A genuinely stale saved pointer and
matching route still clear together.

These tests lock in:
  1. ``api()`` attaches HTTP context (``.status``, ``.statusText``, ``.body``)
     to thrown errors so callers can branch on status without re-parsing text.
  2. ``loadSession()`` clears only route/localStorage values that still name a
     missing requested ID; boot must preserve an unrelated saved restore target.
  3. The server 404s a deleted *WebUI* session on ``GET /api/session`` instead
     of synthesising a read-only CLI stub, so ``GET`` and the ``POST`` write
     paths agree on whether a session exists and the client can self-heal
     (#2782). A genuine CLI-origin session still returns 200 after its sidecar
     is gone.
"""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from tests.test_issue3993_session_load_self_heal import NODE, _run_load_session_failures


REPO = Path(__file__).parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _run_final_fresh_boot_fallback(saved_session):
    assert NODE, "node is required"
    boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    final_fallback = boot.index("  // no saved session - show empty state, wait for user to hit +")
    start = boot.index("  const _freshPanelPref=", final_fallback)
    end = boot.index("\n  syncWorkspacePanelState();", start)
    snippet = boot[start:end]
    source = f"""
const vm=require('node:vm');
const snippet={json.dumps(snippet)};
const savedSession={json.dumps(saved_session)};
const values=new Map([['hermes-webui-workspace-panel-pref','open']]);
if(savedSession!==null) values.set('hermes-webui-session',savedSession);
const localStorage={{
  getItem(key){{return values.has(key)?values.get(key):null;}},
  setItem(key,value){{values.set(key,String(value));}},
  removeItem(key){{values.delete(key);}}
}};
const bindCalls=[];
const context=vm.createContext({{
  localStorage,prefillIntent:null,
  _workspacePanelMode:'closed',
  _isCompactWorkspaceViewport:()=>false,
  _maybeBindFreshDefaultWorkspaceSession:async(intent)=>{{
    bindCalls.push(intent);
    localStorage.setItem('hermes-webui-session','fresh-session');
    return true;
  }}
}});
(async()=>{{
  await vm.runInContext('(async()=>{{'+snippet+'}})()',context);
  process.stdout.write(JSON.stringify({{
    saved:localStorage.getItem('hermes-webui-session'),
    bindCalls:bindCalls.length
  }}));
}})().catch(error=>{{process.stderr.write(error.stack);process.exit(1);}});
"""
    out = subprocess.run([NODE, "-e", source], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def _api_body() -> str:
    m = re.search(r"async function api\(path,opts=.*?\n\}", WORKSPACE_JS, re.DOTALL)
    assert m, "api() function must exist in workspace.js"
    return m.group(0)


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


def test_missing_route_clears_only_route_and_boot_keeps_other_saved_session():
    """A route-only 404 removes that route while retaining a different valid
    saved session, including across boot's outer restore catch."""
    data = _run_load_session_failures(
        statuses=[404, 500], sid="missing-session", route="/session/missing-session",
        saved="valid-session", run_boot_catch=True,
    )
    assert data["requests"] == ["missing-session", "valid-session"]
    assert data["saved"] == "valid-session"
    assert data["routeSid"] is None
    assert data["pathname"] == "/"


def test_final_boot_fallback_preserves_saved_restore_target_but_binds_fresh_boot():
    """Auto-binding must not overwrite a surviving saved target after route 404."""
    restored = _run_final_fresh_boot_fallback("valid-session")
    assert restored["saved"] == "valid-session"
    assert restored["bindCalls"] == 0

    fresh = _run_final_fresh_boot_fallback(None)
    assert fresh["saved"] == "fresh-session"
    assert fresh["bindCalls"] == 1


def test_genuine_saved_session_404_clears_matching_route_and_pointer():
    """A saved ID and route that both name the missing session are both stale."""
    data = _run_load_session_failures(
        statuses=[404], sid="missing-session", route="/session/missing-session",
        saved="missing-session", run_boot_catch=True,
    )
    assert data["requests"] == ["missing-session"]
    assert data["saved"] is None
    assert data["routeSid"] is None


def test_missing_saved_pointer_preserves_unrelated_route():
    """A stale saved pointer must not remove a route that names another target."""
    data = _run_load_session_failures(
        statuses=[404], sid="missing-session", route="/session/valid-session",
        saved="missing-session", run_boot_catch=True,
    )
    assert data["requests"] == ["missing-session"]
    assert data["saved"] is None
    assert data["routeSid"] == "valid-session"
    assert data["pathname"] == "/session/valid-session"


def test_clicking_missing_other_session_preserves_active_navigation():
    """A 404 from a different target must not clear the live session's state."""
    data = _run_load_session_failures(
        statuses=[404], sid="missing-session", route="/session/valid-session",
        saved="valid-session", current_sid="valid-session",
    )
    assert data["requests"] == ["missing-session"]
    assert data["saved"] == "valid-session"
    assert data["routeSid"] == "valid-session"


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
