"""Regression coverage for issue #4766: `/api/sessions` still supports sidebar-source pushdown.

The backend route still honors `sidebar_source` for narrow fetches such as
`show_cli_sessions=false`, while the #3418 sidebar UI now filters origins
client-side after loading the combined payload.
"""

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


def _session_rows(
    webui_count,
    cli_count,
    archived_webui_count=0,
    archived_cli_count=0,
    start=0,
):
    rows = []
    for index in range(webui_count):
        rows.append(
            {
                "session_id": f"webui-{start + index}",
                "title": "WebUI Session",
                "profile": "default",
                "archived": index < archived_webui_count,
                "message_count": 1,
                "updated_at": 1000 + index,
                "last_message_at": 1000 + index,
                "source": "webui",
                "raw_source": "webui",
                "session_source": "webui",
                "source_tag": "webui",
            }
        )
    for index in range(cli_count):
        rows.append(
            {
                "session_id": f"cli-{start + index + 10000}",
                "title": "Imported CLI session",
                "profile": "default",
                "archived": index < archived_cli_count,
                "message_count": 1,
                "updated_at": 2000 + index,
                "last_message_at": 2000 + index,
                "source": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_tag": "cli",
            }
        )
    return rows


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
    enriched = []
    row_ids = {str(row["session_id"]) for row in rows if row.get("session_id")}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: enriched.append([r["session_id"] for r in rows]))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set(row_ids & {str(sid) for sid in ids}))
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return enriched


def test_sidebar_source_webui_excludes_cli_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    enriched = _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 30
    assert all(r["session_id"].startswith("webui-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20
    assert body["archived_count"] == 0
    expected = {
        row["session_id"] for row in rows
        if not row["archived"] and row["session_id"].startswith("webui-")
    }
    assert set(enriched[0]) == expected


def test_sidebar_source_cli_excludes_webui_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 20
    assert all(r["session_id"].startswith("cli-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20


def test_sidebar_source_omitted_returns_all_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 50
    assert len([r for r in body["sessions"] if r["session_id"].startswith("webui-")]) == 30
    assert len([r for r in body["sessions"] if r["session_id"].startswith("cli-")]) == 20


def test_sidebar_source_returns_cross_bucket_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    webui_rows = [r for r in rows if r["session_id"].startswith("webui-")]
    cli_rows = [r for r in rows if r["session_id"].startswith("cli-")]

    body = handler.json_body()
    assert handler.status == 200
    assert body["webui_session_count"] == len(webui_rows)
    assert body["cli_session_count"] == len(cli_rows)


def test_sidebar_source_preserves_archived_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    body = handler.json_body()

    assert handler.status == 200
    assert body["archived_webui_count"] == 2
    assert body["archived_cli_count"] == 3
    assert body["archived_count"] == 5
    assert len([r for r in body["sessions"] if r["archived"]]) == 2


def test_sidebar_source_varies_cache_key():
    key_webui = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="webui",
    )
    key_cli = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="cli",
    )
    key_omitted = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source=None,
    )

    assert key_webui != key_cli
    assert key_webui != key_omitted
    assert key_cli != key_omitted


def test_frontend_sends_sidebar_source_param():
    src = SESSIONS_JS.read_text(encoding="utf-8")

    assert "function _requestedSessionSidebarSource()" in src
    assert "function _sessionListQueryString()" in src
    assert "const sidebarSource = _requestedSessionSidebarSource();" in src
    assert "if (sidebarSource) qs.set('sidebar_source', sidebarSource);" in src
    assert "function _toggleOriginFilter(origin)" in src
    assert "function _ensureOriginFilterDefaults(originOptions)" in src


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_list_query_string_respects_sidebar_source_and_flags():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    requested_source_fn = _extract_function(src, "_requestedSessionSidebarSource")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    query_fn = _extract_function(src, "_sessionListQueryString")
    script = f"""
global.window = {{ _showCliSessions: true }};
global._activeProject = null;
global._sessionSourceFilter = 'cli';
global._showAllProfiles = true;
global._showArchived = false;
{requested_source_fn}
{exclude_hidden_fn}
{query_fn}
const first = _sessionListQueryString();
window._showCliSessions = false;
global._showArchived = true;
const second = _sessionListQueryString();
global._activeProject = '__none__';
global.NO_PROJECT_FILTER = '__none__';
global._showAllProfiles = false;
global._showArchived = false;
const third = _sessionListQueryString();
console.log(JSON.stringify({{ first, second, third }}));
"""
    body = _run_node(script)

    assert body["first"] == "?exclude_hidden=1&all_profiles=1"
    assert body["second"] == "?sidebar_source=webui&exclude_hidden=1&all_profiles=1&include_archived=1"
    assert body["third"] == "?sidebar_source=webui&exclude_hidden=1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_origin_filter_toggle_updates_local_state_without_refetch():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    normalize_origin_fn = _extract_function(src, "_normalizeSidebarOriginId")
    persist_fn = _extract_function(src, "_persistOriginFilters")
    signature_fn = _extract_function(src, "_originFilterSignature")
    ensure_fn = _extract_function(src, "_ensureOriginFilterDefaults")
    fn_toggle = _extract_function(src, "_toggleOriginFilter")
    script = f"""
    const renderCalls = [];
    global._originFiltersHydrated = false;
    global._originFiltersLoadedFromStorage = false;
    global._activeOriginFilters = new Set(['webui', 'cli']);
    global._activeProject = 'demo-project';
    global._selectedSessions = new Set(['first', 'second']);
    global._sessionSelectMode = true;
global.localStorage = {{
  writes: [],
  setItem(key, value) {{
    this.writes.push([key, value]);
  }},
}};
    global.renderSessionListFromCache = () => {{
      renderCalls.push('cache');
    }};
    {normalize_origin_fn}
    {persist_fn}
    {signature_fn}
    {ensure_fn}
    {fn_toggle}
    _toggleOriginFilter('cli');
    const changedAfterToggle = _ensureOriginFilterDefaults([
      {{ id: 'webui' }},
      {{ id: 'cli' }},
      {{ id: 'slack' }},
    ]);
    console.log(JSON.stringify({{
      activeOrigins: Array.from(global._activeOriginFilters),
      originFiltersLoadedFromStorage: global._originFiltersLoadedFromStorage,
      activeProject: global._activeProject,
      selectedSize: global._selectedSessions.size,
      sessionSelectMode: global._sessionSelectMode,
      storageWrites: global.localStorage.writes,
      changedAfterToggle,
      renderCalls,
    }}));
    """
    body = _run_node(script)

    assert body["activeOrigins"] == ["webui"]
    assert body["originFiltersLoadedFromStorage"] is True
    assert body["activeProject"] is None
    assert body["selectedSize"] == 0
    assert body["sessionSelectMode"] is False
    assert body["storageWrites"] == [["hermes-origin-filters", "[\"webui\"]"]]
    assert body["changedAfterToggle"] is False
    assert body["renderCalls"] == ["cache"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_origin_filter_defaults_expand_catalog_without_overriding_saved_choices():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    normalize_origin_fn = _extract_function(src, "_normalizeSidebarOriginId")
    persist_fn = _extract_function(src, "_persistOriginFilters")
    signature_fn = _extract_function(src, "_originFilterSignature")
    ensure_fn = _extract_function(src, "_ensureOriginFilterDefaults")
    script = f"""
    global.window = {{ _showCliSessions: true }};
    global.localStorage = {{
      writes: [],
      setItem(key, value) {{
        this.writes.push([key, value]);
      }},
    }};
    global._activeOriginFilters = new Set(['webui']);
    global._originFiltersHydrated = false;
    global._originFiltersLoadedFromStorage = false;
    {normalize_origin_fn}
    {persist_fn}
    {signature_fn}
    {ensure_fn}
const firstChanged = _ensureOriginFilterDefaults([
  {{ id: 'webui' }},
  {{ id: 'cli' }},
  {{ id: 'slack' }},
]);
const firstOrigins = Array.from(global._activeOriginFilters);
global._activeOriginFilters = new Set(['webui', 'cli']);
global._originFiltersHydrated = true;
global._originFiltersLoadedFromStorage = true;
const secondChanged = _ensureOriginFilterDefaults([
  {{ id: 'webui' }},
  {{ id: 'cli' }},
  {{ id: 'discord' }},
]);
const secondOrigins = Array.from(global._activeOriginFilters);
console.log(JSON.stringify({{
  firstChanged,
  firstOrigins,
  secondChanged,
  secondOrigins,
  writes: global.localStorage.writes,
}}));
"""
    body = _run_node(script)

    assert body["firstChanged"] is True
    assert body["firstOrigins"] == ["webui", "cli", "slack"]
    assert body["secondChanged"] is False
    assert body["secondOrigins"] == ["webui", "cli"]
    assert body["writes"] == [["hermes-origin-filters", "[\"webui\",\"cli\",\"slack\"]"]]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_source_filtered_cache_preserves_hidden_bucket_runtime_state():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    is_cli_fn = _extract_function(src, "_isCliSession")
    remember_source_fn = _extract_function(src, "_rememberSessionListSource")
    remember_streaming_fn = _extract_function(src, "_rememberRenderedStreamingState")
    remember_snapshot_fn = _extract_function(src, "_rememberRenderedSessionSnapshot")
    purge_fn = _extract_function(src, "_purgeStaleInflightEntries")
    mark_fn = _extract_function(src, "_markPollingCompletionUnreadTransitions")
    script = f"""
global._allSessions = [{{
  session_id: 'cli-1',
  source_tag: 'cli',
  raw_source: 'cli',
  session_source: 'cli',
  is_streaming: false,
  message_count: 1,
  last_message_at: 10,
}}];
global._allSessionsScope = {{ sidebarSource: 'cli' }};
global._sessionListSourceById = new Map([['webui-live', 'webui']]);
global._sessionStreamingById = new Map([['webui-live', true]]);
global._sessionListSnapshotById = new Map([['webui-live', {{ message_count: 1, last_message_at: 1 }}]]);
global._sendInProgress = false;
global._sendInProgressSid = null;
global.INFLIGHT = {{ 'webui-live': {{ lastAssistantText: 'working' }} }};
const cleared = [];
global.clearInflightState = sid => cleared.push(sid);
global._isSessionEffectivelyStreaming = s => Boolean(s.is_streaming);
global._getSessionObservedStreaming = () => ({{}});
global._hasPendingUserMessageSignal = () => false;
global._isSessionActivelyViewedForList = () => false;
global._markSessionCompletionUnread = () => {{}};
global._setSessionViewedCount = () => {{}};
global._rememberObservedStreamingSession = () => {{}};
global._forgetObservedStreamingSession = () => {{}};
{is_cli_fn}
{remember_source_fn}
{remember_streaming_fn}
{remember_snapshot_fn}
{purge_fn}
{mark_fn}
const cliStale = {{
  session_id: 'cli-stale',
  source_tag: 'cli',
  raw_source: 'cli',
  session_source: 'cli',
  is_streaming: false,
  message_count: 2,
  last_message_at: 2,
}};
_rememberRenderedStreamingState(cliStale, true);
_rememberRenderedSessionSnapshot(cliStale);
INFLIGHT['cli-stale'] = {{ lastAssistantText: 'stale' }};
_purgeStaleInflightEntries();
_markPollingCompletionUnreadTransitions(global._allSessions);
console.log(JSON.stringify({{
  inflightKeys: Object.keys(INFLIGHT),
  cleared,
  streamingKeys: Array.from(_sessionStreamingById.keys()).sort(),
  snapshotKeys: Array.from(_sessionListSnapshotById.keys()).sort(),
  sourceKeys: Array.from(_sessionListSourceById.keys()).sort(),
}}));
"""
    body = _run_node(script)

    assert body["inflightKeys"] == ["webui-live"]
    assert body["cleared"] == ["cli-stale"]
    assert "webui-live" in body["streamingKeys"]
    assert "cli-stale" not in body["streamingKeys"]
    assert "webui-live" in body["snapshotKeys"]
    assert "cli-stale" not in body["snapshotKeys"]
    assert "webui-live" in body["sourceKeys"]
    assert "cli-stale" not in body["sourceKeys"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_sid_only_source_remembering_skips_scope_fallback():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    is_cli_fn = _extract_function(src, "_isCliSession")
    remember_source_fn = _extract_function(src, "_rememberSessionListSource")
    script = f"""
global._allSessions = [];
global._allSessionsScope = {{ sidebarSource: 'cli' }};
global._sessionListSourceById = new Map();
{is_cli_fn}
{remember_source_fn}
_rememberSessionListSource(null, 'detached-sid', false);
console.log(JSON.stringify({{
  hasDetached: _sessionListSourceById.has('detached-sid'),
  remembered: Array.from(_sessionListSourceById.entries()),
}}));
"""
    body = _run_node(script)

    assert body["hasDetached"] is False
    assert body["remembered"] == []


def test_session_list_response_omits_bucket_counts_when_missing(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)
    monkeypatch.setattr(routes, "_sidebar_session_response_item", lambda row, *, redact_enabled=None: row)

    body = routes._session_list_payload_to_response(
        {
            "sessions": [{"session_id": "webui-1", "title": "WebUI Session"}],
            "cli_count": 0,
            "archived_count": 0,
            "archived_webui_count": 0,
            "archived_cli_count": 0,
            "include_archived": False,
            "all_profiles": False,
            "active_profile": "default",
            "other_profile_count": 0,
        }
    )

    assert "webui_session_count" not in body
    assert "cli_session_count" not in body
    assert body["sessions"][0]["session_id"] == "webui-1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scope_mismatch_error_path_respects_sidebar_source():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    purge_fn = _extract_function(src, "_purgeStaleInflightEntries")
    requested_source_fn = _extract_function(src, "_requestedSessionSidebarSource")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    query_fn = _extract_function(src, "_sessionListQueryString")
    refresh_fn = _extract_function(src, "_runRenderSessionListRefresh").replace(
        "function _runRenderSessionListRefresh",
        "async function _runRenderSessionListRefresh",
        1,
    )
    script = f"""
global.window = {{ _showCliSessions: true }};
global._showAllProfiles = false;
global._showArchived = false;
global._sessionListHasLoadedOnce = true;
global._SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
global._renderSessionListGen = 1;
global._profileSwitchListEmbargo = false;
global._pendingSessionListPayload = null;
global._allProjects = [];
global._contentSearchResults = ['stale'];
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global.S = {{ activeProfile: 'default' }};
global.$ = () => ({{ value: '' }});
global._isSessionListUserInteracting = () => false;
global._schedulePendingSessionListApply = () => {{}};
    global._showSessionListLoadError = error => {{
      global._lastError = error.message;
    }};
    const renders = [];
    const cleared = [];
    global.renderSessionListFromCache = () => {{
      _purgeStaleInflightEntries();
      renders.push({{
        sessions: Array.isArray(global._allSessions) ? global._allSessions.map(s => s.session_id) : null,
        scope: global._allSessionsScope ? {{ ...global._allSessionsScope }} : null,
        skeleton: global._sessionListSkeletonActive,
        inflightKeys: Object.keys(global.INFLIGHT || {{}}).sort(),
      }});
    }};
    global.api = () => Promise.reject(new Error('boom'));
    global.clearInflightState = sid => cleared.push(sid);
    {purge_fn}
    {requested_source_fn}
    {exclude_hidden_fn}
    {query_fn}
    {refresh_fn}
async function runCase(showCliSessions, cachedSource) {{
  global.window._showCliSessions = showCliSessions;
  global._allSessions = [{{ session_id: cachedSource + '-1' }}];
  global._allSessionsScope = {{
    profile: 'default',
    allProfiles: false,
    sidebarSource: cachedSource,
    excludeHidden: true,
  }};
  global._sessionListSourceById = new Map([['webui-live', 'webui']]);
  global.INFLIGHT = {{ 'webui-live': {{ lastAssistantText: 'working' }} }};
  cleared.length = 0;
  global._sessionListSkeletonActive = true;
  global._lastError = null;
  renders.length = 0;
  await _runRenderSessionListRefresh({{}}, 1);
  return {{
    sessions: Array.isArray(global._allSessions) ? global._allSessions.map(s => s.session_id) : null,
    scope: global._allSessionsScope ? {{ ...global._allSessionsScope }} : null,
    skeleton: global._sessionListSkeletonActive,
    error: global._lastError,
    cleared: [...cleared],
    inflightKeys: Object.keys(global.INFLIGHT || {{}}).sort(),
    render: renders[0] || null,
  }};
}}
(async () => {{
  const mismatch = await runCase(false, null);
  const match = await runCase(true, null);
  console.log(JSON.stringify({{ mismatch, match }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""
    body = _run_node(script)

    assert body["mismatch"]["sessions"] == []
    assert body["mismatch"]["scope"] == {
        "profile": "default",
        "allProfiles": False,
        "sidebarSource": "webui",
        "excludeHidden": True,
    }
    assert body["mismatch"]["skeleton"] is False
    assert body["mismatch"]["render"]["sessions"] == []
    assert body["mismatch"]["inflightKeys"] == []
    assert body["mismatch"]["cleared"] == ["webui-live"]
    assert body["mismatch"]["render"]["inflightKeys"] == []
    assert body["match"]["sessions"] == ["null-1"]
    assert body["match"]["scope"] == {
        "profile": "default",
        "allProfiles": False,
        "sidebarSource": None,
        "excludeHidden": True,
    }
    assert body["match"]["render"]["sessions"] == ["null-1"]


def test_payload_row_count_regression(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")
    body = handler.json_body()

    assert handler.status == 200
    assert len(body["sessions"]) == 30
