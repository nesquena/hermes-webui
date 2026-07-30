"""Regression coverage for issue #4775: hide zero-message and default_hidden rows for default sidebar loads."""

import io
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _session_rows():
    return [
        {
            "session_id": "visible-with-message",
            "title": "Visible message",
            "profile": "default",
            "archived": False,
            "message_count": 3,
            "updated_at": 1000,
            "last_message_at": 1000,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
        {
            "session_id": "hidden-by-default",
            "title": "Default hidden",
            "profile": "default",
            "archived": False,
            "message_count": 1,
            "updated_at": 900,
            "last_message_at": 900,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": True,
        },
        {
            "session_id": "zero-message",
            "title": "Plain zero message",
            "profile": "default",
            "archived": False,
            "message_count": 0,
            "updated_at": 800,
            "last_message_at": 800,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
        {
            "session_id": "active-stream-row",
            "title": "Streaming row",
            "profile": "default",
            "archived": False,
            "message_count": 0,
            "active_stream_id": "stream-active",
            "updated_at": 700,
            "last_message_at": 700,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
        {
            "session_id": "attention-row",
            "title": "Attention row",
            "profile": "default",
            "archived": False,
            "message_count": 0,
            "updated_at": 600,
            "last_message_at": 600,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
        {
            "session_id": "pending-user-row",
            "title": "Pending user row",
            "profile": "default",
            "archived": False,
            "message_count": 0,
            "pending_user_message": "user typing",
            "updated_at": 500,
            "last_message_at": 500,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
        {
            "session_id": "pending-flag-row",
            "title": "Pending flag row",
            "profile": "default",
            "archived": False,
            "message_count": 0,
            "has_pending_user_message": True,
            "updated_at": 550,
            "last_message_at": 550,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "default_hidden": False,
        },
    ]


def _handle_sessions(url):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


def _extract_function(source_text, function_name):
    marker = f"function {function_name}("
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


def _run_node(script):
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@pytest.fixture(autouse=True)
def _clear_cache():
    routes._session_list_cache_clear()
    yield
    routes._session_list_cache_clear()


def _install_common_monkeypatches(monkeypatch, rows):
    session_ids = {str(row["session_id"]) for row in rows if row.get("session_id")}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(
        routes,
        "_session_attention_summary",
        lambda session_id: {"kind": "clarify", "count": 1}
        if str(session_id) == "attention-row"
        else None,
    )
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set(ids) & session_ids)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return rows


def test_default_sidebar_excludes_hidden_and_plain_zero_message_rows(monkeypatch):
    rows = _session_rows()
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1")
    body = handler.json_body()

    assert handler.status == 200
    assert [r["session_id"] for r in body["sessions"]] == [
        "visible-with-message",
        "active-stream-row",
        "attention-row",
        "pending-flag-row",
        "pending-user-row",
    ]


def test_zero_message_rows_with_visibility_signals_survive(monkeypatch):
    rows = _session_rows()
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1")
    body = handler.json_body()
    by_id = {row["session_id"]: row for row in body["sessions"]}

    assert handler.status == 200
    assert set(by_id) >= {
        "active-stream-row",
        "attention-row",
        "pending-flag-row",
        "pending-user-row",
    }
    assert by_id["active-stream-row"]["active_stream_id"] == "stream-active"
    assert by_id["attention-row"]["attention"]["kind"] == "clarify"
    assert by_id["pending-flag-row"]["has_pending_user_message"] is True
    assert by_id["pending-user-row"]["title"] == "Pending user row"


def test_omit_exclude_hidden_still_returns_default_hidden_rows(monkeypatch):
    rows = _session_rows()
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")
    body = handler.json_body()

    assert handler.status == 200
    session_ids = {row["session_id"] for row in body["sessions"]}
    assert "hidden-by-default" in session_ids


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_default_and_unassigned_queries_send_exclude_hidden(monkeypatch):
    src = SESSIONS_JS.read_text(encoding="utf-8")
    requested_source_fn = _extract_function(src, "_requestedSessionSidebarSource")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    project_filter_fn = _extract_function(src, "_setActiveProjectFilter")
    query_fn = _extract_function(src, "_sessionListQueryString")
    script = f"""
global.window = {{ _showCliSessions: false }};
global._showCliSessions = false;
global._showAllProfiles = false;
global._showArchived = false;
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global.renderSessionListFromCache = () => {{}};
global.renderSessionList = () => Promise.resolve();
{requested_source_fn}
{exclude_hidden_fn}
{project_filter_fn}
{query_fn}
const default_query = _sessionListQueryString();
_setActiveProjectFilter('__none__');
const unassigned_query = _sessionListQueryString();
_setActiveProjectFilter('demo-project');
const named_project_query = _sessionListQueryString();
console.log(JSON.stringify({{ default_query, unassigned_query, named_project_query }}));
"""
    body = _run_node(script)

    assert body["default_query"] == "?sidebar_source=webui&exclude_hidden=1"
    assert body["unassigned_query"] == "?sidebar_source=webui&exclude_hidden=1"
    assert body["named_project_query"] == "?sidebar_source=webui"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_filter_click_path_triggers_fresh_session_load():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    project_filter_fn = _extract_function(src, "_setActiveProjectFilter")
    script = f"""
const calls = [];
global.NO_PROJECT_FILTER = '__none__';
global._activeProject = null;
global.renderSessionListFromCache = () => {{
  calls.push('cache');
}};
global.renderSessionList = (opts) => {{
  calls.push(opts);
  return Promise.resolve();
}};
{project_filter_fn}
_setActiveProjectFilter('demo-project');
_setActiveProjectFilter('__none__');
console.log(JSON.stringify({{
  activeProject: global._activeProject,
  calls,
}}));
"""
    body = _run_node(script)

    assert body["activeProject"] == "__none__"
    assert body["calls"] == [
        "cache",
        {"deferWhileInteracting": False},
        "cache",
        {"deferWhileInteracting": False},
    ]


def test_cache_key_varies_for_exclude_hidden_and_visible_only():
    key_without_filters = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="webui",
    )
    key_exclude_hidden = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        sidebar_source="webui",
    )
    key_visible_only = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        visible_only=True,
        sidebar_source="webui",
    )

    assert key_without_filters != key_exclude_hidden
    assert key_without_filters != key_visible_only
    assert key_exclude_hidden != key_visible_only
