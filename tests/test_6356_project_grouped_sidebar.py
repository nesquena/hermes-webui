"""Focused coverage for the presentation-only grouped sidebar projection."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run_node_json(script, *, cwd=ROOT):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".cjs", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    finally:
        script_path.unlink(missing_ok=True)


def _require_playwright():
    if sync_playwright is None:
        pytest.skip("playwright is unavailable; upstream CI installs it")
    return sync_playwright


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_projection_orders_pinned_projects_and_unassigned():
    match = re.search(
        r"function _buildSessionSidebarGroups\(.*?\n\}\n\nfunction renderSessionListFromCache",
        SESSIONS_JS,
        re.S,
    )
    assert match, "group projection helper is missing"
    helper = match.group(0).rsplit("\n\nfunction renderSessionListFromCache", 1)[0]
    script = f"""
const getBucket = (ts) => ts < 20 ? 'Today' : 'Older';
const getTimestamp = (s) => s.ts;
const _sessionTimeBucketLabel = getBucket;
const _sessionSortTimestampMs = getTimestamp;
const t = (key) => key === 'sidebar_group_unassigned' ? 'Unassigned' : key;
{helper}
const rows = [
  {{session_id:'pinned', pinned:true, project_id:'p1', ts:1}},
  {{session_id:'alpha', project_id:'p1', ts:2}},
  {{session_id:'beta', project_id:'p2', ts:3}},
  {{session_id:'none', project_id:null, ts:4}},
];
const projects = [{{project_id:'p2', name:'Beta'}}, {{project_id:'p1', name:'Alpha'}}];
const grouped = _buildSessionSidebarGroups(rows, true, projects, 0);
const flat = _buildSessionSidebarGroups(rows, false, projects, 0);
const collapsedVisible = grouped.flatMap(g => g.collapseKey === 'project:p1' ? [] : g.items.map(s => s.session_id));
console.log(JSON.stringify({{grouped:grouped.map(g=>g.label), flat:flat.map(g=>g.label), collapsedVisible}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    observed = json.loads(result.stdout)
    assert observed["grouped"] == ["★ Pinned", "Beta", "Alpha", "Unassigned"]
    assert observed["flat"] == ["★ Pinned", "Today"]
    assert observed["collapsedVisible"] == ["pinned", "beta", "none"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_collapse_keeps_hidden_rows_out_of_visible_ids_select_all_and_virtual_totals():
    script = """
__GROUPS__
__SELECT_ALL__
__SWIPE__
__RENDER__
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.style = {
      setProperty() {},
      removeProperty() {},
    };
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.scrollTop = 0;
    this.clientHeight = 520;
    this.parentNode = null;
    this.classList = {
      add() {},
      remove() {},
      toggle() {},
    };
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  insertBefore(child, reference) {
    child.parentNode = this;
    const index = this.children.indexOf(reference);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }
  set innerHTML(_value) {
    this.children = [];
  }
  get firstChild() {
    return this.children[0] || null;
  }
  addEventListener() {}
  setAttribute(name, value) {
    this[name] = value;
  }
  closest() {
    return null;
  }
}
const elements = new Map();
const sessionSearch = new Element('input');
const sessionList = new Element('div');
const batchActionBar = new Element('div');
sessionSearch.value = '';
elements.set('sessionSearch', sessionSearch);
elements.set('sessionList', sessionList);
elements.set('batchActionBar', batchActionBar);
const document = {
  createElement(tagName) { return new Element(tagName); },
  querySelectorAll() { return []; },
  body: new Element('body'),
};
function $(id) { return elements.get(id); }
const localState = new Map([
  ['hermes-date-groups-collapsed', JSON.stringify({'project:p1': true})],
]);
const localStorage = {
  getItem(key) { return localState.has(key) ? localState.get(key) : null; },
  setItem(key, value) { localState.set(key, String(value)); },
};
const window = globalThis;
const ICONS = new Proxy({}, {get: () => ''});
function li() { return ''; }
window._sidebarGroupByProject = true;
window._showCliSessions = false;
window._projectQuickCreate = false;
let _sessionVisibleSidebarIds = [];
let _selectedSessions = new Set();
let _allProjects = [
  {project_id: 'p1', name: 'Alpha'},
  {project_id: 'p2', name: 'Beta'},
];
let _allSessions = [
  {session_id: 'alpha', project_id: 'p1', ts: 1},
  {session_id: 'beta', project_id: 'p2', ts: 2},
];
let _sessionListSkeletonActive = false;
let _renamingSid = null;
let _sessionActionMenu = null;
let _sessionSourceFilter = 'webui';
let _contentSearchResults = [];
let _serverWebuiSessionCount = null;
let _serverCliSessionCount = null;
let _sessionListRefreshAnimationPending = false;
let _sessionListEnterAllAnimationPending = false;
let _sessionSelectMode = false;
let _sessionListLoadError = null;
let _activeProject = null;
let _otherProfileCount = 0;
let _showAllProfiles = false;
let _showArchived = false;
let _archivedRowsLoadedLimit = 0;
let _archivedCliCount = 0;
let _archivedWebuiCount = 0;
let _pendingSessionReflowPositions = null;
let _expandedChildSessionKeys = new Set();
const _sessionSwipeReturnOffsets = new Map();
const NO_PROJECT_FILTER = '__none__';
const SESSION_SWIPE_DURATION_MS = 0;
const SESSION_SWIPE_REFLOW_LEAD_MS = 0;
const SESSION_VIRTUAL_ROW_HEIGHT = 32;
const SESSION_VIRTUAL_BUFFER_ROWS = 0;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 0;
const SESSION_ARCHIVED_PAGE_SIZE = 25;
const SESSION_ARCHIVED_MAX_LOADED_LIMIT = 100;
const SESSION_LIST_FLIP_TIMEOUT_MS = 0;
const SESSION_REFLOW_TIMEOUT_MS = 0;
function closeSessionActionMenu() {}
function _purgeStaleInflightEntries() {}
function _activeSessionIdForSidebar() { return null; }
function _sessionRowsWithActiveEphemeralSession(rows) { return rows; }
function _sessionSearchMergeMatches(rows) { return rows; }
function _ensureActiveSessionRowPresent(rows) { return rows; }
function _partitionSidebarSessionRows(rows) {
  return {
    cliSessionCount: 0,
    profileFiltered: rows,
    sessionsRaw: rows,
    archivedCount: 0,
    webuiReferenceRaw: [],
    cliReferenceRaw: [],
    webuiSessionsRaw: rows,
    cliSessionsRaw: [],
  };
}
function _scopedSidebarReferenceRows() { return []; }
function _renderSidebarRowsFromRawSessions(rows) { return rows; }
function _sessionSourceTabCount(_filter, webuiCount) { return webuiCount ?? 0; }
function _syncSidebarExpansionForActiveSession() {}
function _sessionPrefersReducedMotion() { return true; }
function _serverNowMs() { return 0; }
function _sessionSidebarSortCompare(a, b) { return (a.ts || 0) - (b.ts || 0); }
function _sidebarLineageKeyForRow(session) { return session.session_id; }
function _isReadOnlySession() { return false; }
function _ensureSessionVirtualScrollHandler() {}
function _sessionLineageContainsSession() { return false; }
function _isSessionEffectivelyStreaming() { return false; }
function _rememberRenderedStreamingState() {}
function _rememberRenderedSessionSnapshot() {}
function _hasUnreadForSession() { return false; }
function _sessionAttentionState() { return {}; }
function _sessionDisplayTitle(s) { return s.session_id; }
function _sessionTitleTags() { return []; }
function _sessionTimestampMs(s) { return s.ts || 0; }
function _formatRelativeSessionTime() { return ''; }
function _isMessagingSession() { return false; }
function _sessionSegmentCount() { return 0; }
function _sessionLineageBadgeTooltip() { return ''; }
function _sessionForkTooltip() { return ''; }
function _sessionFullTitleTooltip() { return ''; }
function _sourceKeyForSession() { return 'webui'; }
function _getChannelLabel() { return ''; }
function _truncatedSessionId(s) { return s.session_id; }
function _sessionTitleForForkParent(s) { return s.session_id; }
function _lineageSegmentsForRender() { return []; }
function _lineageReportCacheKey() { return ''; }
function _sessionSearchContentPreview() { return ''; }
function _buildSessionRenameStarter() { return () => {}; }
function _sessionStateTooltip() { return ''; }
function _sessionSortTimestampMs(s) { return s.ts || 0; }
function _sessionTimeBucketLabel() { return 'Today'; }
function _sessionVirtualWindow(opts) {
  return {
    total: opts.total,
    start: 0,
    end: opts.total,
    virtualized: true,
    topPad: 0,
    itemHeight: opts.itemHeight,
  };
}
function _sessionVirtualSpacer(height, where) {
  const spacer = new Element('div');
  spacer.dataset.height = String(height);
  spacer.dataset.where = where;
  return spacer;
}
function _resyncSessionVirtualWindowAfterRender() {}
function _sessionArchivePagingFilterActive() { return false; }
function _isCliSession() { return false; }
function toggleSessionSelectMode() {}
function _playSessionRowsReflowFromPositions() {}
function _updateBatchActionBar() {}
function _bindGroupedProjectDropTarget() {}
function t(key) { return key; }
renderSessionListFromCache();
selectAllSessions();
const initialVisible = [..._sessionVisibleSidebarIds];
const initialTotal = sessionList.dataset.sessionVirtualTotal;
const groupedHeaders = sessionList.children.filter(wrapper => wrapper.className.startsWith('session-date-group')).map(wrapper => wrapper.children[0].children[1].textContent);
window._sidebarGroupByProject = false;
localState.set('hermes-date-groups-collapsed', '{}');
renderSessionListFromCache();
const flatHeaders = sessionList.children.filter(wrapper => wrapper.className.startsWith('session-date-group')).map(wrapper => wrapper.children[0].children[1].textContent);
window._sidebarGroupByProject = true;
window.matchMedia = query => ({matches: query === '(any-pointer: coarse)'});
renderSessionListFromCache();
const coarseRow = sessionList.children.find(child => child.className.startsWith('session-date-group')).children[1].children[0];
const coarseChildren = coarseRow.children.map(child => child.className).filter(className => className.includes('session-swipe-affordance'));
process.stdout.write(JSON.stringify({
  visible: initialVisible,
  selected: Array.from(_selectedSessions),
  total: initialTotal,
  groupedHeaders,
  flatHeaders,
  coarseChildren,
}));
"""
    script = script.replace("__GROUPS__", _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups"))
    script = script.replace("__SELECT_ALL__", _extract_js_function(SESSIONS_JS, "selectAllSessions"))
    script = script.replace("__SWIPE__", _extract_js_function(SESSIONS_JS, "_makeSessionSwipeAffordance"))
    script = script.replace("__RENDER__", _extract_js_function(SESSIONS_JS, "renderSessionListFromCache"))
    observed = _run_node_json(script)
    assert observed == {
        "visible": ["beta"],
        "selected": ["beta"],
        "total": "1",
        "groupedHeaders": ["Alpha", "Beta"],
        "flatHeaders": ["Today"],
        "coarseChildren": [
            "session-swipe-affordance session-swipe-affordance-right",
            "session-swipe-affordance session-swipe-affordance-left",
        ],
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_locale_switch_rerenders_grouped_sidebar_strings_immediately():
    match = re.search(
        r"langSel\.addEventListener\('change',function\(\)\{(?P<body>.*?)\},\{once:false\}\);",
        PANELS_JS,
        re.S,
    )
    assert match, "language change handler is missing"
    body = match.group("body")
    assert "renderSessionListFromCache" in body
    grouped_helper = _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups")
    drag_tooltip_line = _extract_js_line(SESSIONS_JS, "dragHandle.title=")
    script = """
const document = {documentElement: {}, querySelectorAll: () => []};
const localStorage = {getItem: () => null, setItem: () => {}};
__I18N__
__GROUPED_HELPER__
let applied = 0;
let renders = 0;
let autosaves = 0;
const groupedHeader = {textContent: ''};
const dragHandle = {title: ''};
function applyLocaleToDOM() { applied += 1; }
function renderSessionListFromCache() {
  renders += 1;
  const groups = _buildSessionSidebarGroups(
    [{session_id: 'unassigned', project_id: null, ts: 1}], true, [], 0,
  );
  groupedHeader.textContent = groups[0].label;
  __DRAG_TOOLTIP_LINE__
}
function _schedulePreferencesAutosave() { autosaves += 1; }
const langSel = {value: 'de'};
const localeSwitch = function() { __HANDLER__ };
localeSwitch.call(langSel);
process.stdout.write(JSON.stringify({
  applied, renders, autosaves, groupedHeader, dragHandle,
  expectedHeader: LOCALES.de.sidebar_group_unassigned,
  expectedDragTooltip: LOCALES.de.sidebar_group_drag_to_project,
}));
""".replace("__I18N__", I18N_JS).replace("__GROUPED_HELPER__", grouped_helper).replace(
        "__DRAG_TOOLTIP_LINE__", drag_tooltip_line
    ).replace("__HANDLER__", body)
    observed = _run_node_json(script)
    assert observed["applied"] == 1
    assert observed["renders"] == 1
    assert observed["autosaves"] == 1
    assert observed["groupedHeader"]["textContent"] == observed["expectedHeader"]
    assert observed["dragHandle"]["title"] == observed["expectedDragTooltip"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_appearance_autosave_response_cannot_revert_grouping_choice():
    queue_helper = _extract_js_function(PANELS_JS, "_queueAppearanceSettingsWrite")
    autosave = _extract_js_function(PANELS_JS, "_autosaveAppearanceSettings")
    script = f"""
const pending = [];
const window = {{}};
const localStorage = {{setItem() {{}}}};
let renders = 0;
let _appearanceAutosaveGeneration = 0;
let _appearanceAutosaveWriteQueue = Promise.resolve();
let _settingsAppearanceAutosaveRetryPayload = null;
let persisted = null;
function api(_url, options) {{
  const body = JSON.parse(options.body);
  return new Promise(resolve => pending.push(() => {{ persisted = body; resolve(body); }}));
}}
function _rememberAppearanceSaved() {{}}
function _setAppearanceAutosaveStatus() {{}}
function renderSessionListFromCache() {{ renders += 1; }}
{queue_helper}
{autosave}
(async () => {{
  const oldRequest = _autosaveAppearanceSettings({{sidebar_group_by_project: false}}, 1);
  _appearanceAutosaveGeneration = 2;
  const newRequest = _autosaveAppearanceSettings({{sidebar_group_by_project: true}}, 2);
  await Promise.resolve();
  if (pending.length !== 1) throw new Error('newer write bypassed the serialized queue');
  pending[0]();
  await new Promise(resolve => setTimeout(resolve, 0));
  if (pending.length !== 2) throw new Error('newer write was not queued after the older write');
  pending[1]();
  await newRequest;
  await oldRequest;
  process.stdout.write(JSON.stringify({{grouped: window._sidebarGroupByProject, renders, persisted}}));
}})();
"""
    assert _run_node_json(script) == {
        "grouped": True,
        "renders": 1,
        "persisted": {"sidebar_group_by_project": True},
    }


def test_grouped_drag_move_routes_through_session_move_api():
    assert "api('/api/session/move'" in SESSIONS_JS
    assert "_bindGroupedProjectDropTarget(hdr,g.project||null,g.label)" in SESSIONS_JS
    assert "project:{project_id:projectId}" in SESSIONS_JS
    assert "_groupedProjectProfileHides(session.profile,targetProject.profile)" in SESSIONS_JS
    assert "SESSION_PROJECT_DRAG_MIME='application/x-hermes-webui-session-id'" in SESSIONS_JS
    assert "SESSION_PROJECT_DRAG_TEXT_PREFIX='hermes-webui-session:'" in SESSIONS_JS
    assert "_setSessionProjectDragData(e.dataTransfer,s.session_id)" in SESSIONS_JS
    assert "const sid=_sessionProjectDragSid(e.dataTransfer);" in SESSIONS_JS
    assert "/api/session/move" in SESSIONS_JS
    assert "session files" not in SESSIONS_JS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_drag_provenance_requires_branded_sidebar_drag():
    script = """
__DRAG_DECLS__
__DRAG_FUNCS__
class FakeDataTransfer {
  constructor(typesArr = [], dataMap = {}) {
    this._map = {...dataMap};
    this.types = [...typesArr];
  }
  getData(mime) { return this._map[mime] || ''; }
  setData(mime, value) {
    this._map[mime] = value;
    if (!this.types.includes(mime)) this.types.push(mime);
  }
}
const native = new FakeDataTransfer();
_setSessionProjectDragData(native, 'alpha');
const nativeObserved = {
  types: [...native.types].sort(),
  custom: native.getData(SESSION_PROJECT_DRAG_MIME),
  plain: native.getData('text/plain'),
};
const stripped = new FakeDataTransfer(['text/plain'], {'text/plain': nativeObserved.plain});
const strippedAccepted = _isSessionProjectMoveDrag(stripped);
const strippedSid = _sessionProjectDragSid(stripped);
_clearSessionProjectDragData();
const foreignCustom = new FakeDataTransfer(
  ['application/x-hermes-webui-session-id'],
  {[SESSION_PROJECT_DRAG_MIME]: 'alpha'},
);
const foreignCustomAccepted = _isSessionProjectMoveDrag(foreignCustom);
const foreignCustomSid = _sessionProjectDragSid(foreignCustom);
const activeCustom = new FakeDataTransfer(
  ['application/x-hermes-webui-session-id'],
  {[SESSION_PROJECT_DRAG_MIME]: 'beta'},
);
_setSessionProjectDragData(activeCustom, 'beta');
const activeCustomAccepted = _isSessionProjectMoveDrag(activeCustom);
const activeCustomSid = _sessionProjectDragSid(activeCustom);
const foreign = new FakeDataTransfer(['text/plain'], {'text/plain': 'alpha'});
const foreignAccepted = _isSessionProjectMoveDrag(foreign);
const foreignSid = _sessionProjectDragSid(foreign);
process.stdout.write(JSON.stringify({nativeObserved, strippedAccepted, strippedSid, foreignCustomAccepted, foreignCustomSid, activeCustomAccepted, activeCustomSid, foreignAccepted, foreignSid}));
"""
    script = script.replace(
        "__DRAG_DECLS__",
        "\n".join(
            _extract_js_line(SESSIONS_JS, prefix)
            for prefix in (
                "const SESSION_PROJECT_DRAG_MIME=",
                "const SESSION_PROJECT_DRAG_TEXT_PREFIX=",
                "let _activeSidebarProjectDragSessionId=",
            )
        ),
    )
    script = script.replace(
        "__DRAG_FUNCS__",
        "\n".join(
            _extract_js_function(SESSIONS_JS, name)
            for name in (
                "_setSessionProjectDragData",
                "_clearSessionProjectDragData",
                "_sessionProjectDragSid",
                "_isSessionProjectMoveDrag",
            )
        ),
    )
    observed = _run_node_json(script)
    assert observed == {
        "nativeObserved": {
            "types": ["application/x-hermes-webui-session-id", "text/plain"],
            "custom": "alpha",
            "plain": "hermes-webui-session:alpha",
        },
        "strippedAccepted": True,
        "strippedSid": "alpha",
        "foreignCustomAccepted": False,
        "foreignCustomSid": "",
        "activeCustomAccepted": True,
        "activeCustomSid": "beta",
        "foreignAccepted": False,
        "foreignSid": "",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_drag_cleanup_clears_abandoned_state_before_foreign_drag():
    script = """
const listeners = {};
const window = {
  _sessionProjectDragCleanupBound: false,
  addEventListener(name, callback) { listeners[name] = callback; },
};
class FakeDataTransfer {
  constructor(typesArr = [], dataMap = {}) {
    this.types = [...typesArr];
    this._map = {...dataMap};
  }
  getData(mime) { return this._map[mime] || ''; }
  setData(mime, value) {
    this._map[mime] = value;
    if (!this.types.includes(mime)) this.types.push(mime);
  }
}
__DRAG_DECLS__
__DRAG_FUNCS__
function brandedDragIsAccepted() {
  return _isSessionProjectMoveDrag(new FakeDataTransfer(['text/plain'], {
    'text/plain': 'hermes-webui-session:drop',
  }));
}
async function run() {
  const observed = {};
  _setSessionProjectDragData(new FakeDataTransfer(), 'drop');
  listeners.dragend();
  observed.dragend = brandedDragIsAccepted();
  _setSessionProjectDragData(new FakeDataTransfer(), 'drop');
  listeners.pagehide();
  observed.pagehide = brandedDragIsAccepted();
  _setSessionProjectDragData(new FakeDataTransfer(), 'drop');
  listeners.blur();
  observed.blur = brandedDragIsAccepted();
  _setSessionProjectDragData(new FakeDataTransfer(), 'drop');
  listeners.drop();
  observed.dropBeforeTick = brandedDragIsAccepted();
  await new Promise(resolve => setTimeout(resolve, 0));
  observed.dropAfterTick = brandedDragIsAccepted();
  process.stdout.write(JSON.stringify(observed));
}
run().catch(error => { console.error(error); process.exit(1); });
"""
    script = script.replace(
        "__DRAG_DECLS__",
        "\n".join(
            _extract_js_line(SESSIONS_JS, prefix)
            for prefix in (
                "const SESSION_PROJECT_DRAG_MIME=",
                "const SESSION_PROJECT_DRAG_TEXT_PREFIX=",
                "let _activeSidebarProjectDragSessionId=",
            )
        ),
    )
    script = script.replace(
        "__DRAG_FUNCS__",
        "\n".join(
            _extract_js_function(SESSIONS_JS, name)
            for name in (
                "_setSessionProjectDragData",
                "_clearSessionProjectDragData",
                "_bindSessionProjectDragCleanup",
                "_isSessionProjectMoveDrag",
            )
        )
        + "\n_bindSessionProjectDragCleanup();",
    )
    assert _run_node_json(script) == {
        "dragend": False,
        "pagehide": False,
        "blur": False,
        "dropBeforeTick": True,
        "dropAfterTick": False,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_grouped_move_leaves_cache_and_render_unchanged():
    script = """
let apiCalls = [];
let renders = 0;
let toasts = [];
let _allSessions = [{session_id: 'failed', project_id: 'source'}];
async function api(url, options) {
  apiCalls.push({url, body: JSON.parse(options.body)});
  throw new Error('blocked');
}
function renderSessionListFromCache() { renders += 1; }
function showToast(message) { toasts.push(message); }
__MOVE__
(async () => {
  const result = await _moveSessionToProject(_allSessions[0], 'target', 'Target');
  process.stdout.write(JSON.stringify({
    result,
    apiCalls,
    renders,
    toasts,
    session: _allSessions[0],
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    script = script.replace("__MOVE__", _extract_js_function(SESSIONS_JS, "_moveSessionToProject"))
    observed = _run_node_json(script)
    assert observed == {
        "result": False,
        "apiCalls": [
            {
                "url": "/api/session/move",
                "body": {"session_id": "failed", "project_id": "target"},
            }
        ],
        "renders": 0,
        "toasts": ["Move failed: blocked"],
        "session": {"session_id": "failed", "project_id": "source"},
    }


def test_grouped_drag_does_not_steal_coarse_pointer_or_flat_view_interactions():
    match = re.search(
        r"const _groupedFinePointer\s*=\s*window\._sidebarGroupByProject.*?;\s*"
        r"const _hasCoarsePointer\s*=\s*window\.matchMedia.*?\(any-pointer:\s*coarse\).*?;\s*"
        r"if\s*\(!readOnly\s*&&\s*\(!_groupedFinePointer\s*\|\|\s*_hasCoarsePointer\)\)\s*\{",
        SESSIONS_JS,
        re.S,
    )
    assert match, "grouped drag affordance guard is missing"
    script = """
const keepSwipe = (readOnly, groupedFinePointer, hasCoarsePointer) =>
  Boolean(!readOnly && (!groupedFinePointer || hasCoarsePointer));
if (keepSwipe(false, true, false)) throw new Error('fine grouped pointer should hide swipe affordance');
if (!keepSwipe(false, true, true)) throw new Error('hybrid pointer should keep swipe affordance');
if (!keepSwipe(false, false, true)) throw new Error('coarse pointer should keep swipe affordance');
if (keepSwipe(true, true, true)) throw new Error('read-only row should not keep swipe affordance');
"""
    subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    assert re.search(r"el\.onpointerdown\s*=.*?if\s*\(e\.pointerType\s*===\s*['\"]touch['\"]\)\s*return", SESSIONS_JS, re.S)
    assert "el.addEventListener('touchstart'" in SESSIONS_JS
    assert "_buildSessionSidebarGroups(orderedSessions,!!window._sidebarGroupByProject" in SESSIONS_JS


def test_grouped_drag_affordance_is_hover_revealed_and_unassigned_header_is_styled_target():
    assert re.search(
        r"\.session-item:hover \.session-project-drag-handle,\s*"
        r"\.session-item:focus-within \.session-project-drag-handle\{display:inline-block;\}",
        (ROOT / "static" / "style.css").read_text(encoding="utf-8"),
    )
    assert ".session-project-drag-handle{display:none;" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "Object.prototype.hasOwnProperty.call(g,'dropProjectId')?' project-session-header':''" in SESSIONS_JS
    assert ".project-session-header.drag-over" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _extract_js_function(source, name):
    marker = f"async function {name}" if f"async function {name}" in source else f"function {name}"
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _extract_js_line(source, prefix):
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped
    raise AssertionError(f"JavaScript line not found: {prefix}")


def test_grouped_drag_playwright_covers_move_eligibility_and_redraw():
    playwright_factory = _require_playwright()
    helpers = "\n".join(
        [_extract_js_line(SESSIONS_JS, prefix) for prefix in (
            "const SESSION_PROJECT_DRAG_MIME=",
            "const SESSION_PROJECT_DRAG_TEXT_PREFIX=",
            "let _activeSidebarProjectDragSessionId=",
        )]
        + [
            _extract_js_function(SESSIONS_JS, name)
            for name in (
                "_setSessionProjectDragData",
                "_clearSessionProjectDragData",
                "_sessionProjectDragSid",
                "_isSessionProjectMoveDrag",
                "_groupedProjectProfileHides",
                "_moveSessionToProject",
                "_handleGroupedProjectDrop",
                "_bindGroupedProjectDropTarget",
            )
        ]
    )
    fixture = """
const _allSessions = [
  {session_id:'compatible', project_id:'source', profile:'alpha'},
  {session_id:'to-unassigned', project_id:'source', profile:'alpha'},
  {session_id:'same', project_id:'target', profile:'alpha'},
  {session_id:'failed', project_id:'source', profile:'alpha'},
  {session_id:'cross-profile', project_id:'source', profile:'alpha'},
  {session_id:'fallback', project_id:'source', profile:'alpha'},
];
const apiCalls = [];
let renderCount = 0;
const toasts = [];
async function api(url, options) {
  const body = JSON.parse(options.body);
  apiCalls.push({url, body});
  if (body.project_id === 'failed-target') throw new Error('failed-target blocked');
  return {ok:true};
}
function renderSessionListFromCache() { renderCount += 1; }
function showToast(message) { toasts.push(message); }
""" + helpers + """
for (const spec of [
  ['compatible-target', {project_id:'target', profile:'alpha'}, 'Compatible'],
  ['same-target', {project_id:'target', profile:'alpha'}, 'Same'],
  ['failed-target', {project_id:'failed-target', profile:'alpha'}, 'Failed'],
  ['cross-profile-target', {project_id:'beta', profile:'beta'}, 'Cross profile'],
  ['fallback-target', {project_id:'fallback-project', profile:'alpha'}, 'Fallback'],
  ['unassigned-target', null, 'Unassigned'],
]) {
  const header = document.createElement('div');
  header.dataset.target = spec[0];
  document.body.appendChild(header);
  _bindGroupedProjectDropTarget(header, spec[1], spec[2]);
}
window.runGroupedDragProof = async () => {
  const observed = {};
  const compatible = new DataTransfer();
  _setSessionProjectDragData(compatible, 'compatible');
  observed.compatibleTypes = [...compatible.types].sort();
  observed.compatibleCustom = compatible.getData(SESSION_PROJECT_DRAG_MIME);
  observed.compatiblePlain = compatible.getData('text/plain');
  const protectedDragoverData = new DataTransfer();
  protectedDragoverData.setData(SESSION_PROJECT_DRAG_MIME, 'compatible');
  Object.defineProperty(protectedDragoverData, 'getData', {value: () => ''});
  const dragover = new DragEvent('dragover', {
    bubbles: true,
    cancelable: true,
    dataTransfer: protectedDragoverData,
  });
  const compatibleHeader = document.querySelector('[data-target="compatible-target"]');
  observed.dragoverDefaultPrevented = !compatibleHeader.dispatchEvent(dragover);
  observed.dragoverClass = compatibleHeader.classList.contains('drag-over');
  document.querySelector('[data-target="compatible-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:compatible})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  const same = new DataTransfer();
  _setSessionProjectDragData(same, 'same');
  document.querySelector('[data-target="same-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:same})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  const failed = new DataTransfer();
  _setSessionProjectDragData(failed, 'failed');
  document.querySelector('[data-target="failed-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:failed})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  const crossProfile = new DataTransfer();
  _setSessionProjectDragData(crossProfile, 'cross-profile');
  document.querySelector('[data-target="cross-profile-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:crossProfile})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  const fallbackNative = new DataTransfer();
  _setSessionProjectDragData(fallbackNative, 'fallback');
  const fallbackPlain = fallbackNative.getData('text/plain');
  const fallback = new DataTransfer();
  fallback.setData('text/plain', fallbackPlain);
  document.querySelector('[data-target="fallback-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:fallback})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  const unassigned = new DataTransfer();
  _setSessionProjectDragData(unassigned, 'to-unassigned');
  document.querySelector('[data-target="unassigned-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:unassigned})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  _clearSessionProjectDragData();
  const foreign = new DataTransfer();
  foreign.setData('text/plain', 'compatible');
  document.querySelector('[data-target="compatible-target"]').dispatchEvent(
    new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:foreign})
  );
  await new Promise(resolve => setTimeout(resolve, 0));

  observed.fallbackPlain = fallbackPlain;
  return {observed, apiCalls, renderCount, toasts, sessions:_allSessions};
};
"""
    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=fixture)
        observed = page.evaluate("window.runGroupedDragProof()")
        browser.close()
    assert observed["observed"] == {
        "compatibleTypes": ["application/x-hermes-webui-session-id", "text/plain"],
        "compatibleCustom": "compatible",
        "compatiblePlain": "hermes-webui-session:compatible",
        "dragoverDefaultPrevented": True,
        "dragoverClass": True,
        "fallbackPlain": "hermes-webui-session:fallback",
    }
    assert observed["apiCalls"] == [
        {
            "url": "/api/session/move",
            "body": {"session_id": "compatible", "project_id": "target"},
        },
        {
            "url": "/api/session/move",
            "body": {"session_id": "failed", "project_id": "failed-target"},
        },
        {
            "url": "/api/session/move",
            "body": {"session_id": "fallback", "project_id": "fallback-project"},
        },
        {
            "url": "/api/session/move",
            "body": {"session_id": "to-unassigned", "project_id": None},
        },
    ]
    assert observed["renderCount"] == 3
    assert observed["toasts"] == [
        "Moved to Compatible",
        "Move failed: failed-target blocked",
        "Moved to Fallback",
        "Removed from project",
    ]
    assert next(item for item in observed["sessions"] if item["session_id"] == "compatible")["project_id"] == "target"
    assert next(item for item in observed["sessions"] if item["session_id"] == "failed")["project_id"] == "source"
    assert next(item for item in observed["sessions"] if item["session_id"] == "fallback")["project_id"] == "fallback-project"
    assert next(item for item in observed["sessions"] if item["session_id"] == "to-unassigned")["project_id"] is None
