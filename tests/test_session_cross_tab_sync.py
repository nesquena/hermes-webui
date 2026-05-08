"""Regression tests for cross-tab active session behavior."""
import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_sessions_js_listens_for_active_session_storage_changes():
    assert "addEventListener('storage'" in SESSIONS_JS or 'addEventListener("storage"' in SESSIONS_JS
    assert "hermes-webui-session" in SESSIONS_JS
    assert "_handleActiveSessionStorageEvent" in SESSIONS_JS


def test_storage_event_does_not_globally_switch_tabs():
    handler_pos = SESSIONS_JS.find("async function _handleActiveSessionStorageEvent")
    assert handler_pos != -1
    next_pos = SESSIONS_JS.find("if(typeof window", handler_pos)
    assert next_pos != -1
    block = SESSIONS_JS[handler_pos:next_pos]
    assert "loadSession(sid)" not in block
    assert "Each tab owns its" in block
    assert "renderSessionListFromCache" in block


def test_session_switch_updates_url_path_for_tab_local_anchor():
    assert "function _sessionIdFromLocation()" in SESSIONS_JS
    assert "function _setActiveSessionUrl(sid)" in SESSIONS_JS
    assert "'/session/'" in SESSIONS_JS
    assert "_setActiveSessionUrl(S.session.session_id)" in SESSIONS_JS
    assert "_setActiveSessionUrl(S.session.session_id)" in COMMANDS_JS
    assert "addEventListener('popstate'" in SESSIONS_JS or 'addEventListener("popstate"' in SESSIONS_JS


def test_boot_prefers_url_session_over_local_storage_session():
    assert "const urlSession=(typeof _sessionIdFromLocation==='function')?_sessionIdFromLocation():null;" in BOOT_JS
    assert "const savedLocal=localStorage.getItem('hermes-webui-session');" in BOOT_JS
    assert "const saved=urlSession||savedLocal;" in BOOT_JS
    assert "if(!urlSession&&savedLocal&&await _savedSessionShouldStaySidebarOnly(savedLocal))" in BOOT_JS


def test_api_helper_resolves_against_document_base_not_session_path():
    workspace_js = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "new URL(rel,document.baseURI||location.href)" in workspace_js
    assert "new URL(rel,location.href)" not in workspace_js


def test_long_lived_stream_urls_resolve_against_document_base():
    for rel in ("static/messages.js", "static/boot.js", "static/terminal.js"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "document.baseURI||location.href" in src


def test_session_url_route_serves_index_and_base_href_handles_session_path():
    assert 'parsed.path.startswith("/session/")' in ROUTES_PY
    assert "marker='/session/'" in INDEX_HTML
    assert "path.slice(0,i+1)" in INDEX_HTML


def _evaluate_base_href_for_path(path: str) -> str:
    script_match = re.search(r"<script>\(function\(\).*?</script>", INDEX_HTML)
    assert script_match, "index.html should include the dynamic base href script"
    script = script_match.group(0).removeprefix("<script>").removesuffix("</script>")
    node = f"""
const location={{origin:'https://example.test', pathname:{json.dumps(path)}}};
let written='';
const document={{write:(s)=>{{written+=s;}}}};
{script}
console.log(written);
"""
    return subprocess.check_output(["node", "-e", node], text=True).strip()


def test_base_href_resolution_handles_session_urls_under_subpath_mounts():
    assert _evaluate_base_href_for_path("/session/abc123") == '<base href="https://example.test/">'
    assert _evaluate_base_href_for_path("/myapp/session/abc123") == '<base href="https://example.test/myapp/">'
    assert _evaluate_base_href_for_path("/myapp/session/abc123/extra") == '<base href="https://example.test/myapp/">'
    assert _evaluate_base_href_for_path("/session-tools/session/abc123") == '<base href="https://example.test/session-tools/">'
    assert _evaluate_base_href_for_path("/session-tools/page") == '<base href="https://example.test/session-tools/">'
