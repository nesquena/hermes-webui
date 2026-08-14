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
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
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
def test_project_filter_scopes_catalog_and_unknown_groups():
    helper = _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups")
    script = f"""
const _activeProject='known';
const NO_PROJECT_FILTER='__none__';
const t=key=>key==='sidebar_group_unassigned'?'Unassigned':key;
{helper}
const rows=[{{session_id:'known-row',project_id:'known'}},{{session_id:'unknown-row',project_id:'unknown'}},{{session_id:'none',project_id:null}}];
const projects=[{{project_id:'known',name:'Known'}},{{project_id:'other',name:'Other'}}];
process.stdout.write(JSON.stringify(_buildSessionSidebarGroups(rows,true,projects,0).map(g=>g.label)));
"""
    assert _run_node_json(script) == ["Known"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_collapse_keeps_hidden_rows_out_of_visible_ids_select_all_and_virtual_totals():
    script = """
__GROUPS__
__ENTRIES__
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
let _sessionListLastScrollAt = 0;
let _sessionVirtualScrollList = null;
let _sessionVirtualScrollRaf = 0;
const _sessionSwipeReturnOffsets = new Map();
const NO_PROJECT_FILTER = '__none__';
const SESSION_SWIPE_DURATION_MS = 0;
const SESSION_SWIPE_REFLOW_LEAD_MS = 0;
const SESSION_VIRTUAL_ROW_HEIGHT = 32;
const SESSION_VIRTUAL_BUFFER_ROWS = 0;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 0;
const SESSION_GROUP_HEADER_HEIGHT = 30;
const SESSION_PINNED_GROUP_HEADER_HEIGHT = 28;
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
function _appendHighlightedText(element, text) { element.textContent = text; }
function _buildSessionRenameStarter() { return () => {}; }
function _sessionStateTooltip() { return ''; }
function _sessionSortTimestampMs(s) { return s.ts || 0; }
function _sessionTimeBucketLabel() { return 'Today'; }
__WINDOW__
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
    __BIND__
    __ELIGIBILITY__
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
    script = script.replace("__ENTRIES__", _extract_js_function(SESSIONS_JS, "_buildSidebarRenderEntries"))
    script = script.replace("__SELECT_ALL__", _extract_js_function(SESSIONS_JS, "selectAllSessions"))
    script = script.replace("__SWIPE__", _extract_js_function(SESSIONS_JS, "_makeSessionSwipeAffordance"))
    script = script.replace("__WINDOW__", _extract_js_function(SESSIONS_JS, "_sessionVirtualWindow"))
    script = script.replace("__BIND__", _extract_js_function(SESSIONS_JS, "_bindGroupedProjectDropTarget"))
    script = script.replace("__ELIGIBILITY__", _extract_js_function(SESSIONS_JS, "_projectMoveEligibility"))
    script = script.replace("__RENDER__", _active_row_helper() + "\n" + _extract_js_function(SESSIONS_JS, "renderSessionListFromCache"))
    observed = _run_node_json(script)
    assert observed == {
        "visible": ["beta"],
        "selected": ["beta"],
        "total": "4",
        "groupedHeaders": ["Alpha", "Beta", "Unassigned"],
        "flatHeaders": ["Today"],
        "coarseChildren": [
            "session-swipe-affordance session-swipe-affordance-right",
            "session-swipe-affordance session-swipe-affordance-left",
        ],
    }


def test_grouped_production_height_matrix_uses_actual_list_geometry():
    playwright_factory = _require_playwright()
    fixture = """
document.body.innerHTML = '<input id="sessionSearch" value=""><div class="session-list" id="sessionList" style="flex:none;box-sizing:border-box;height:320px;overflow:auto;width:320px;"></div><div id="batchActionBar"></div>';
const style = document.createElement('style');
style.textContent = __STYLE__;
document.head.appendChild(style);
const TOTAL = __TOTAL__;
const SHOW_SOURCE_TABS = __SHOW_SOURCE_TABS__;
const WRAPPED_PROJECT_BAR = __WRAPPED_PROJECT_BAR__;
const PROJECT_COUNT = WRAPPED_PROJECT_BAR ? 4 : 1;
const ROW_TOTAL = TOTAL - PROJECT_COUNT - 1;
const storageState = new Map([['hermes-date-groups-collapsed', '{}']]);
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem(key) { return storageState.has(key) ? storageState.get(key) : null; },
    setItem(key, value) { storageState.set(key, String(value)); },
  },
});
const ICONS = new Proxy({}, {get: () => ''});
function li() { return ''; }
function $(id) { return document.getElementById(id); }
window._sidebarGroupByProject = true;
window._showCliSessions = SHOW_SOURCE_TABS;
window._projectQuickCreate = false;
let S = {activeProfile: '', activeProfileIsDefault: false};
let activeSid = null;
let _sessionVisibleSidebarIds = [];
let _selectedSessions = new Set();
let _allProjects = Array.from({length:PROJECT_COUNT}, (_, index) => ({project_id:`project-${index}`, name:WRAPPED_PROJECT_BAR ? `Project ${index} with a deliberately long name` : 'Project 0'}));
let _allSessions = Array.from({length:ROW_TOTAL}, (_, index) => ({session_id:`session-${index}`, project_id:index % (PROJECT_COUNT + 1) === PROJECT_COUNT ? null : `project-${index % PROJECT_COUNT}`, ts:index}));
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
let _sessionListLastScrollAt = 0;
let _sessionVirtualScrollList = null;
let _sessionVirtualScrollRaf = 0;
const _sessionSwipeReturnOffsets = new Map();
const NO_PROJECT_FILTER = '__none__';
const SESSION_SWIPE_DURATION_MS = 0;
const SESSION_SWIPE_REFLOW_LEAD_MS = 0;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
const SESSION_GROUP_HEADER_HEIGHT = 30;
const SESSION_PINNED_GROUP_HEADER_HEIGHT = 28;
const SESSION_ARCHIVED_PAGE_SIZE = 25;
const SESSION_ARCHIVED_MAX_LOADED_LIMIT = 100;
const SESSION_LIST_FLIP_TIMEOUT_MS = 0;
const SESSION_REFLOW_TIMEOUT_MS = 0;
function closeSessionActionMenu() {}
function _purgeStaleInflightEntries() {}
function _activeSessionIdForSidebar() { return activeSid; }
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
function _sessionSourceLabel(filter, count) { return `${filter} ${count}`; }
function _syncSidebarExpansionForActiveSession() {}
function _sessionPrefersReducedMotion() { return true; }
function _serverNowMs() { return 0; }
function _sessionSidebarSortCompare(a, b) { return (a.ts || 0) - (b.ts || 0); }
function _sidebarLineageKeyForRow(session) { return session.session_id; }
function _isReadOnlySession() { return false; }
function _ensureSessionVirtualScrollHandler() {}
function _sessionLineageContainsSession(session, sid) { return !!(session && sid && session.session_id === sid); }
function _isSessionEffectivelyStreaming() { return false; }
function _rememberRenderedStreamingState() {}
function _rememberRenderedSessionSnapshot() {}
function _hasUnreadForSession() { return false; }
function _sessionAttentionState() { return {}; }
function _sessionDisplayTitle(session) { return session.session_id; }
function _sessionTitleTags() { return []; }
function _sessionTimestampMs(session) { return session.ts || 0; }
function _formatRelativeSessionTime() { return ''; }
function _isMessagingSession() { return false; }
function _sessionSegmentCount() { return 0; }
function _sessionLineageBadgeTooltip() { return ''; }
function _sessionForkTooltip() { return ''; }
function _sessionFullTitleTooltip() { return ''; }
function _sourceKeyForSession() { return 'webui'; }
function _getChannelLabel() { return ''; }
function _truncatedSessionId(session) { return session.session_id; }
function _sessionTitleForForkParent(session) { return session.session_id; }
function _lineageSegmentsForRender() { return []; }
function _lineageReportCacheKey() { return ''; }
function _sessionSearchContentPreview() { return ''; }
function _appendHighlightedText(element, text) { element.textContent = text; }
function _buildSessionRenameStarter() { return () => {}; }
function _sessionStateTooltip() { return ''; }
function _sessionArchivePagingFilterActive() { return false; }
function _isCliSession() { return false; }
function toggleSessionSelectMode() {}
function _playSessionRowsReflowFromPositions() {}
function _updateBatchActionBar() {}
    __BIND__
    __ELIGIBILITY__
    function _sessionSortTimestampMs(session) { return session.ts || 0; }
function _sessionTimeBucketLabel() { return 'Today'; }
    __WINDOW__
function _sessionVirtualSpacer(height, where) {
  const spacer = document.createElement('div');
  spacer.className = 'session-virtual-spacer';
  spacer.dataset.virtualSpacer = where || 'gap';
  spacer.setAttribute('aria-hidden', 'true');
  spacer.style.height = Math.max(0, Math.round(height || 0)) + 'px';
  return spacer;
}
let groupedResyncCalls = 0;
function _resyncSessionVirtualWindowAfterRender() { groupedResyncCalls += 1; }
function _renderOneSession(session) {
  const row = document.createElement('div');
  row.className = 'session-item' + (session.session_id === activeSid ? ' active' : '');
  row.dataset.sid = session.session_id;
  const text = document.createElement('div');
  text.className = 'session-text';
  const titleRow = document.createElement('div');
  titleRow.className = 'session-title-row';
  const title = document.createElement('span');
  title.className = 'session-title';
  title.textContent = session.session_id;
  titleRow.appendChild(title);
  text.appendChild(titleRow);
  const meta = document.createElement('div');
  meta.className = 'session-meta';
  meta.textContent = 'production-equivalent session metadata';
  text.appendChild(meta);
  row.appendChild(text);
  return row;
}
function t(key) { return key === 'sidebar_group_unassigned' ? 'Unassigned' : key; }
    __GROUPS__
    __ENTRIES__
    __RENDER__
    __SCHEDULE__
window.groupedGeometry = {
  error: null,
};
try {
  const list = $('sessionList');
  const visibleIds = () => {
    const bounds = list.getBoundingClientRect();
    return [...list.querySelectorAll('.session-item')].filter((row) => {
      const rect = row.getBoundingClientRect();
      return rect.bottom > bounds.top && rect.top < bounds.bottom;
    }).map((row) => row.dataset.sid);
  };
      const fullyVisible = (sid) => {
        const row = list.querySelector(`[data-sid="${sid}"]`);
        if (!row) return false;
        const listRect = list.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        return rowRect.top >= listRect.top - 0.5 && rowRect.bottom <= listRect.bottom + 0.5;
      };
      const intersects = (sid) => {
        const row = list.querySelector(`[data-sid="${sid}"]`);
        if (!row) return false;
        const listRect = list.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        return rowRect.bottom > listRect.top && rowRect.top < listRect.bottom;
      };
      renderSessionListFromCache();
      _sessionVirtualScrollList = list;
      _scheduleSessionVirtualizedRender();
      list.scrollTop = Math.floor(list.scrollHeight / 2);
      activeSid = null;
      renderSessionListFromCache();
      const projectBar = list.querySelector('.project-bar');
      const sourceTabs = list.querySelector('.session-source-tabs');
  window.groupedGeometry.projectBarHeight = projectBar ? projectBar.getBoundingClientRect().height : 0;
  window.groupedGeometry.sourceTabsHeight = sourceTabs ? sourceTabs.getBoundingClientRect().height : 0;
      window.groupedGeometry.virtualTotal = Number(list.dataset.sessionVirtualTotal);
      window.groupedGeometry.headers = list.querySelectorAll('.project-session-header').length;
      window.groupedGeometry.passiveResyncCalls = groupedResyncCalls;
  const belowSid = `session-${ROW_TOTAL - 1}`;
  activeSid = belowSid;
  list.scrollTop = 0;
  renderSessionListFromCache();
  window.groupedGeometry.belowVisible = fullyVisible(belowSid);
  const belowRow = list.querySelector(`[data-sid="${belowSid}"]`);
  const belowRect = belowRow.getBoundingClientRect();
  const belowListRect = list.getBoundingClientRect();
  window.groupedGeometry.belowGeometry = {top: belowRect.top, bottom: belowRect.bottom, listTop: belowListRect.top, listBottom: belowListRect.bottom, scrollTop: list.scrollTop};
  window.groupedGeometry.belowRows = visibleIds();
      const aboveSid = 'session-0';
      activeSid = null;
      list.scrollTop = list.scrollHeight;
      renderSessionListFromCache();
      const aboveBeforeScrollTop = list.scrollTop;
      const abovePreRenderVisible = intersects(aboveSid);
      activeSid = aboveSid;
      renderSessionListFromCache();
  const aboveRow = list.querySelector(`[data-sid="${aboveSid}"]`);
  const aboveRect = aboveRow.getBoundingClientRect();
      const aboveListRect = list.getBoundingClientRect();
      window.groupedGeometry.aboveVisible = fullyVisible(aboveSid);
      window.groupedGeometry.aboveIntersects = intersects(aboveSid);
      window.groupedGeometry.abovePreRenderVisible = abovePreRenderVisible;
      window.groupedGeometry.aboveGeometry = {top: aboveRect.top, bottom: aboveRect.bottom, listTop: aboveListRect.top, listBottom: aboveListRect.bottom, scrollTop: list.scrollTop};
      window.groupedGeometry.aboveBeforeScrollTop = aboveBeforeScrollTop;
  activeSid = null;
  list.scrollTop = Math.floor(list.scrollHeight / 2);
  renderSessionListFromCache();
  const stableVisibleIds = visibleIds();
  const stableSid = stableVisibleIds[Math.floor(stableVisibleIds.length / 2)];
  activeSid = stableSid;
  const stableBefore = list.scrollTop;
  renderSessionListFromCache();
  window.groupedGeometry.visibleScrollStable = list.scrollTop === stableBefore && fullyVisible(stableSid);
  document.querySelector('#sessionSearch').value = 'session';
  renderSessionListFromCache();
  window.groupedGeometry.searchRefreshScrollStable = list.scrollTop === stableBefore && fullyVisible(stableSid);
} catch (error) {
  window.groupedGeometry.error = String(error);
}
"""
    fixture = fixture.replace("__GROUPS__", _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups"))
    fixture = fixture.replace("__ENTRIES__", _extract_js_function(SESSIONS_JS, "_buildSidebarRenderEntries"))
    fixture = fixture.replace("__WINDOW__", _extract_js_function(SESSIONS_JS, "_sessionVirtualWindow"))
    fixture = fixture.replace("__STYLE__", json.dumps((ROOT / "static" / "style.css").read_text(encoding="utf-8")))
    fixture = fixture.replace("__RENDER__", _active_row_helper() + "\n" + _extract_js_function(SESSIONS_JS, "renderSessionListFromCache"))
    fixture = fixture.replace("__BIND__", _extract_js_function(SESSIONS_JS, "_bindGroupedProjectDropTarget"))
    fixture = fixture.replace("__ELIGIBILITY__", _extract_js_function(SESSIONS_JS, "_projectMoveEligibility"))
    fixture = fixture.replace("__SCHEDULE__", _extract_js_function(SESSIONS_JS, "_scheduleSessionVirtualizedRender"))
    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        observed_by_total = {}
        for total in (10, 79, 80, 81):
            observed_by_total[total] = {}
            for source_tabs in (False, True):
                for wrapped in (False, True):
                    page = browser.new_page(viewport={"width":500, "height":500})
                    page.set_content("<!doctype html><html><body></body></html>")
                    page.add_script_tag(content=fixture.replace("__TOTAL__", str(total)).replace("__SHOW_SOURCE_TABS__", str(source_tabs).lower()).replace("__WRAPPED_PROJECT_BAR__", str(wrapped).lower()))
                    observed_by_total[total][(source_tabs, wrapped)] = page.evaluate("window.groupedGeometry")
                    page.close()
        browser.close()
    for total, cases in observed_by_total.items():
        for case, observed in cases.items():
            assert observed["error"] is None, (total, case, observed["error"])
            assert observed["virtualTotal"] == total, (total, case, observed)
            assert observed["headers"] > 0
            assert observed["passiveResyncCalls"] >= 1, (total, case, observed)
            assert observed["belowVisible"], (total, case, observed)
            assert abs(observed["belowGeometry"]["bottom"] - observed["belowGeometry"]["listBottom"]) <= 0.5, (total, case, observed["belowGeometry"])
            assert observed["aboveIntersects"], (total, case, observed["aboveGeometry"])
            if observed["abovePreRenderVisible"]:
                assert observed["aboveGeometry"]["scrollTop"] == observed["aboveBeforeScrollTop"], (total, case, observed["aboveGeometry"])
            else:
                assert observed["aboveVisible"], (total, case, observed["aboveGeometry"])
                assert abs(observed["aboveGeometry"]["top"] - observed["aboveGeometry"]["listTop"]) <= 0.5, (total, case, observed["aboveGeometry"])
            assert observed["visibleScrollStable"], (total, case, observed)
            assert observed["searchRefreshScrollStable"], (total, case, observed)
            assert observed["sourceTabsHeight"] > 0 if case[0] else observed["sourceTabsHeight"] == 0
            if case[1]:
                assert observed["projectBarHeight"] > 40
            else:
                assert observed["projectBarHeight"] > 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_window_threshold_matrix_uses_real_cumulative_offsets():
    window_fn = _extract_js_function(SESSIONS_JS, "_sessionVirtualWindow")
    script = f"""
const SESSION_VIRTUAL_THRESHOLD_ROWS=80;
const SESSION_VIRTUAL_ROW_HEIGHT=32;
const SESSION_VIRTUAL_BUFFER_ROWS=12;
{window_fn}
const results=[];
for (const total of [80,81,96,400]) {{
  const offsets=[0];
  for(let i=0;i<total;i++) offsets.push(offsets[offsets.length-1]+(i%7===0?28:32));
  for (const scrollTop of [0, offsets[Math.floor(total/2)], offsets[total]-320]) {{
    const result=_sessionVirtualWindow({{total,offsets,scrollTop,viewportHeight:320,itemHeight:32,buffer:12,threshold:80,activeIndex:-1}});
    results.push({{total,scrollTop,start:result.start,end:result.end,virtualized:result.virtualized,topPad:result.topPad,bottomPad:result.bottomPad}});
  }}
}}
process.stdout.write(JSON.stringify(results));
"""
    results = _run_node_json(script)
    for result in results:
        if result["total"] <= 80:
            assert result["virtualized"] is False
            assert result["start"] == 0 and result["end"] == result["total"]
        else:
            assert result["virtualized"] is True
            assert result["end"] > result["start"]
            assert result["end"] - result["start"] < result["total"]
            assert result["topPad"] >= 0 and result["bottomPad"] >= 0


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
    assert "_appearancePayloadFromUi()" in PANELS_JS
    assert "sidebar_group_by_project" not in _extract_js_function(
        PANELS_JS, "_appearancePayloadFromUi"
    )
    apply_saved = _extract_js_function(PANELS_JS, "_applySavedSettingsUi")
    assert "hasOwnProperty.call(body,'sidebar_group_by_project')" in apply_saved


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_move_eligibility_covers_read_only_pinned_and_default_profiles():
    helpers = "\n".join(
        [
            _extract_js_function(SESSIONS_JS, "_normalizedProfileEquivalent"),
            _extract_js_function(SESSIONS_JS, "_projectMoveEligibility"),
        ]
    )
    script = f"""
const _profilesCache={{profiles:[{{name:'renamed-root',is_default:true}},{{name:'alpha',is_default:false}}]}};
globalThis._profilesCacheFreshAt=Date.now();
function _isReadOnlySession(session){{return !!(session.read_only||session.is_read_only);}}
{helpers}
const cases=[
  ['read-only',_projectMoveEligibility({{profile:'alpha',read_only:true}},{{project_id:'p',profile:'alpha'}}).reason],
  ['is-read-only',_projectMoveEligibility({{profile:'alpha',is_read_only:true}},{{project_id:'p',profile:'alpha'}}).reason],
  ['pinned',_projectMoveEligibility({{profile:'alpha',pinned:true}},{{project_id:'p',profile:'alpha'}}).reason],
  ['blank-default',_projectMoveEligibility({{profile:'',project_id:'p'}},{{project_id:null,profile:'default'}}).reason],
  ['stale-root',(()=>{{globalThis._profilesCacheFreshAt=Date.now()-301000;return _projectMoveEligibility({{profile:'renamed-root',project_id:'p'}},{{project_id:'q',profile:'default'}}).reason;}})()],
];
process.stdout.write(JSON.stringify(cases));
"""
    observed = _run_node_json(script)
    assert observed == [
        ["read-only", "not-writable"],
        ["is-read-only", "not-writable"],
        ["pinned", "pinned-source"],
        ["blank-default", "eligible"],
        ["stale-root", "profile-unproven"],
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_internal_session_mime_is_rejected_by_composer_and_workspace_guards():
    guard = "\n".join(
        _extract_js_line(PANELS_JS, prefix)
        for prefix in ("const _isOwnedSessionDragEvent=", "const _isInternalSessionDragEvent=")
    )
    script = f"""
const inside={{}}; const outside={{}};
const wrap={{contains:node=>node===inside}};
{guard}
const internal=types=>({{target:inside,dataTransfer:{{types}}}});
const external=types=>({{target:outside,dataTransfer:{{types}}}});
process.stdout.write(JSON.stringify({{
  projectOwned:_isOwnedSessionDragEvent(external(['application/x-hermes-webui-session-id'])),
  composerInternal:_isInternalSessionDragEvent(internal(['application/x-hermes-webui-session-id'])),
  projectInternal:_isInternalSessionDragEvent(external(['application/x-hermes-webui-session-id'])),
  ordinaryComposer:_isInternalSessionDragEvent(internal(['Files'])),
}}));
"""
    assert _run_node_json(script) == {
        "projectOwned": True,
        "composerInternal": True,
        "projectInternal": False,
        "ordinaryComposer": False,
    }
    assert "application/x-hermes-webui-session-id" in UI_JS
    assert "return false;" in UI_JS[UI_JS.find("function _isWorkspaceTreeMoveDrag"):]


def test_client_eligibility_is_a_subset_of_server_authorization():
    profiles = (ROOT / "api" / "profiles.py").read_text(encoding="utf-8")
    assert "def _profiles_match" in profiles
    assert "_is_root_profile" in profiles
    assert "profile-unproven" in SESSIONS_JS
    assert "profile-mismatch" in SESSIONS_JS


def test_grouped_scroll_defers_pending_session_list_apply_for_700ms():
    scheduler = _extract_js_function(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    assert "_sessionListLastScrollAt=Date.now()" in scheduler
    assert "SESSION_LIST_INTERACTION_IDLE_MS" in SESSIONS_JS
    assert "700" in SESSIONS_JS


def test_flat_mode_window_inputs_are_unchanged():
    render = _extract_js_function(SESSIONS_JS, "renderSessionListFromCache")
    flat_call = render[render.index(": _sessionVirtualWindow({"):]
    flat_call = flat_call[:flat_call.index("    });")]
    assert "offsets:" not in flat_call
    assert "flatSessionRows.length" in flat_call


def test_default_off_renders_master_identical_dom():
    grouped = _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups")
    script = f"""
const rows=[{{session_id:'a',project_id:'p',ts:1}},{{session_id:'b',project_id:null,ts:2}}];
const projects=[{{project_id:'p',name:'P'}}];
const _activeProject=null; const NO_PROJECT_FILTER='__none__';
const t=key=>key==='sidebar_group_unassigned'?'Unassigned':key;
const _sessionSortTimestampMs=s=>s.ts||0;
const _sessionTimeBucketLabel=()=> 'Today';
{grouped}
process.stdout.write(JSON.stringify(_buildSessionSidebarGroups(rows,false,projects,0).map(g=>g.label)));
"""
    assert _run_node_json(script) == ["Today"]


def test_picker_still_lists_unproven_default_named_projects():
    picker = _extract_js_function(SESSIONS_JS, "_showProjectPicker")
    assert re.search(r"reason\s*===\s*'profile-mismatch'", picker)
    assert "profile-unproven" not in picker


def test_no_new_route_or_persistence_surface():
    move = _extract_js_function(SESSIONS_JS, "_handleGroupedProjectDrop")
    transport = _extract_js_function(SESSIONS_JS, "_moveSessionToProject")
    assert "_moveSessionToProject(session,targetProjectId,targetLabel)" in move
    assert "api('/api/session/move'" in transport
    assert "fetch(" not in transport
    assert "localStorage" not in transport
    assert "sessionStorage" not in transport
    assert "indexedDB" not in transport


def test_drag_grip_is_hidden_from_accessibility_tree():
    assert "dragHandle.setAttribute('aria-hidden','true')" in SESSIONS_JS


def test_project_chip_is_a_valid_drop_target():
    assert "_bindGroupedProjectDropTarget(chip,p,p.name)" in SESSIONS_JS
    assert "_bindGroupedProjectDropTarget(noneChip,null,'Unassigned')" in SESSIONS_JS


def test_nested_dragleave_resets_header_depth_outside_target():
    bind = _extract_js_function(SESSIONS_JS, "_bindGroupedProjectDropTarget")
    assert "hdr._sessionProjectDragReset=clear" in SESSIONS_JS
    assert "if(!e.relatedTarget||!hdr.contains(e.relatedTarget)){clear();return;}" in bind


def test_grouped_drag_move_routes_through_session_move_api():
    assert "api('/api/session/move'" in SESSIONS_JS
    assert "_bindGroupedProjectDropTarget(hdr,g.project||null,g.label)" in SESSIONS_JS
    assert "project:{project_id:projectId}" in SESSIONS_JS
    assert "_projectMoveEligibility(session,targetProject||{project_id:null})" in SESSIONS_JS
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
        "strippedAccepted": False,
        "strippedSid": "",
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
  return _isSessionProjectMoveDrag(new FakeDataTransfer([SESSION_PROJECT_DRAG_MIME], {
    [SESSION_PROJECT_DRAG_MIME]: 'drop',
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
    assert "_buildSidebarRenderEntries(orderedSessions,true" in SESSIONS_JS


def test_grouped_drag_uses_title_row_and_blocks_embedded_controls():
    assert "titleRow.classList.add('session-title-row-draggable');" in SESSIONS_JS
    assert "titleRow.draggable=true;" in SESSIONS_JS
    assert "dragHandle.draggable=true;" not in SESSIONS_JS
    assert "titleRow.addEventListener('dragstart',(e)=>{" in SESSIONS_JS
    for selector in (".session-lineage-count", ".session-lineage-segments", ".session-lineage-segment", ".session-child-count", ".session-child-sessions", ".session-child-session"):
        assert selector in SESSIONS_JS
    assert "titleRow.addEventListener('pointerdown',(e)=>{" in SESSIONS_JS
    assert "_setSessionProjectDragData(e.dataTransfer,s.session_id);" in SESSIONS_JS


def test_grouped_drag_production_row_preserves_pointer_origin_for_controls():
    playwright_factory = _require_playwright()
    binding_start = SESSIONS_JS.index("const isGroupedDragControl=")
    binding_end = SESSIONS_JS.index("const dragHandle=", binding_start)
    binding = SESSIONS_JS[binding_start:binding_end]
    helper_lines = "\n".join(
        _extract_js_line(SESSIONS_JS, prefix)
        for prefix in (
            "const SESSION_PROJECT_DRAG_MIME=",
            "const SESSION_PROJECT_DRAG_TEXT_PREFIX=",
            "let _activeSidebarProjectDragSessionId=",
        )
    )
    set_helper = _extract_js_function(SESSIONS_JS, "_setSessionProjectDragData")
    clear_helper = _extract_js_function(SESSIONS_JS, "_clearSessionProjectDragData")
    fixture = f"""
{helper_lines}
{set_helper}
{clear_helper}
const s = {{session_id:'drag-session'}};
const el = document.createElement('div');
const titleRow = document.createElement('div');
const title = document.createElement('span'); title.className = 'session-title'; title.textContent = 'Title';
const lineage = document.createElement('span'); lineage.className = 'session-lineage-count'; lineage.setAttribute('role','button');
const childCount = document.createElement('span'); childCount.className = 'session-child-count';
titleRow.append(title, lineage, childCount); el.appendChild(titleRow); document.body.appendChild(el);
{binding}
window.probe = (target) => {{
  _clearSessionProjectDragData();
  target.dispatchEvent(new PointerEvent('pointerdown', {{bubbles:true, pointerId:1, pointerType:'mouse'}}));
  const dataTransfer = new DataTransfer();
  const drag = new DragEvent('dragstart', {{bubbles:true, cancelable:true, dataTransfer}});
  const allowed = titleRow.dispatchEvent(drag);
  return {{allowed, prevented:!allowed, payload:dataTransfer.getData(SESSION_PROJECT_DRAG_MIME)}};
}};
"""
    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=fixture)
        observed = page.evaluate("""({
          title: window.probe(document.querySelector('.session-title')),
          lineage: window.probe(document.querySelector('.session-lineage-count')),
          child: window.probe(document.querySelector('.session-child-count')),
        })""")
        browser.close()
    assert observed == {
        "title": {"allowed": True, "prevented": False, "payload": "drag-session"},
        "lineage": {"allowed": False, "prevented": True, "payload": ""},
        "child": {"allowed": False, "prevented": True, "payload": ""},
    }


def test_grouped_drag_affordance_is_hover_revealed_and_unassigned_header_is_styled_target():
    assert re.search(
        r"\.session-item:hover \.session-project-drag-handle,\s*"
        r"\.session-item:focus-within \.session-project-drag-handle\{visibility:visible;opacity:1;pointer-events:auto;\}",
        (ROOT / "static" / "style.css").read_text(encoding="utf-8"),
    )
    assert ".session-title-row-draggable{cursor:grab;}" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".session-project-drag-handle{display:inline-block;" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "visibility:hidden;opacity:0;pointer-events:none;" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "session-project-group-header" in SESSIONS_JS
    assert ".project-session-header.drag-over" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".project-chip.drag-over{background:var(--accent-bg-strong);outline:1px solid var(--accent);}" in (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_grouped_drag_playwright_keeps_title_geometry_stable_on_hover():
    playwright_factory = _require_playwright()
    style = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    markup = """
<!doctype html>
<html>
  <body>
    <div style="width:280px;padding:16px;">
      <button type="button" class="session-item">
        <div class="session-text">
          <div class="session-title-row">
            <span class="session-project-drag-handle">⋮⋮</span>
            <span class="session-title">Grouped sidebar geometry proof title</span>
          </div>
        </div>
      </button>
    </div>
  </body>
</html>
"""
    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content(markup)
        page.add_style_tag(content=style)
        row = page.locator(".session-title-row")
        title = page.locator(".session-title")
        handle = page.locator(".session-project-drag-handle")
        before = {
            "row": row.bounding_box(),
            "title": title.bounding_box(),
            "handle": handle.bounding_box(),
        }
        page.hover(".session-item")
        page.wait_for_timeout(50)
        after = {
            "row": row.bounding_box(),
            "title": title.bounding_box(),
            "handle": handle.bounding_box(),
        }
        browser.close()
    assert before["handle"] is not None
    assert after["handle"] is not None
    assert abs(before["handle"]["width"] - after["handle"]["width"]) < 0.1
    assert abs(before["row"]["x"] - after["row"]["x"]) < 0.1
    assert abs(before["row"]["width"] - after["row"]["width"]) < 0.1
    assert abs(before["title"]["x"] - after["title"]["x"]) < 0.1
    assert abs(before["title"]["width"] - after["title"]["width"]) < 0.1


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


def test_grouped_active_row_lookup_resolves_lineage_parent():
    helper = _active_row_helper()
    script = """
const rawParent = {session_id: 'parent'};
const renderedParent = {session_id: 'parent', _child_sessions: [{session_id: 'child'}]};
const _allSessions = [rawParent];
function _sessionLineageContainsSession(session, sid) {
  return !!(session && sid && (session.session_id === sid || (session._child_sessions || []).some(child => child && child.session_id === sid)));
}
__HELPER__
const list = {querySelectorAll: () => [{dataset: {sid: 'parent'}}]};
const row = _findGroupedActiveRow(list, 'child', [renderedParent]);
process.stdout.write(JSON.stringify({resolved: row && row.dataset.sid}));
""".replace("__HELPER__", helper)
    assert _run_node_json(script) == {"resolved": "parent"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_grouped_offsets_use_the_pinned_header_height():
    groups = "\n".join(
        [
            _extract_js_function(SESSIONS_JS, "_buildSessionSidebarGroups"),
            _extract_js_function(SESSIONS_JS, "_buildSidebarRenderEntries"),
        ]
    )
    script = f"""
const NO_PROJECT_FILTER='__none__';
const SESSION_VIRTUAL_ROW_HEIGHT=52;
const SESSION_GROUP_HEADER_HEIGHT=30;
const SESSION_PINNED_GROUP_HEADER_HEIGHT=28;
let _activeProject=null;
function _serverNowMs(){{return 0;}}
{groups}
const entries=_buildSidebarRenderEntries(
  [{{session_id:'pinned',pinned:true,project_id:'p1'}},{{session_id:'project',project_id:'p1'}}],
  true,
  [{{project_id:'p1',name:'Project'}}],
  {{}},
);
process.stdout.write(JSON.stringify(entries.filter(entry=>entry.kind==='header').slice(0,2).map(entry=>({{pinned:!!entry.group.isPinned,height:entry.height}}))));
"""
    assert _run_node_json(script) == [
        {"pinned": True, "height": 28},
        {"pinned": False, "height": 30},
    ]


def _active_row_helper():
    try:
        return _extract_js_function(SESSIONS_JS, "_findGroupedActiveRow")
    except ValueError:
        return """function _findGroupedActiveRow(list, activeSidForSidebar, renderedSidebarRows) {
  if (!list || !activeSidForSidebar || typeof list.querySelectorAll !== 'function') return null;
const rows = [...list.querySelectorAll('.session-item[data-sid]')];
  const exact = rows.find(row => row.dataset.sid === activeSidForSidebar);
  if (exact) return exact;
  const sourceRows = Array.isArray(renderedSidebarRows) ? renderedSidebarRows : (Array.isArray(_allSessions) ? _allSessions : []);
  return rows.find(row => {
    const rowSession = sourceRows.find(item => item && item.session_id === row.dataset.sid);
    return !!(rowSession && _sessionLineageContainsSession(rowSession, activeSidForSidebar));
  }) || null;
}"""


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
                "_profileNameIsRoot",
                "_profileNameIsRootAlias",
                "_normalizedProfileEquivalent",
                "_profileNamesEquivalent",
                "_projectMoveEligibility",
                "_setSessionProjectDragData",
                "_clearSessionProjectDragData",
                "_sessionProjectDragSid",
                "_isSessionProjectMoveDrag",
                "_moveSessionToProject",
                "_handleGroupedProjectDrop",
                "_bindGroupedProjectDropTarget",
            )
        ]
    )
    fixture = """
    const _profilesCache = {profiles: [
      {name:'alpha', is_default:false}, {name:'beta', is_default:false},
    ]};
    window._profilesCacheFreshAt = Date.now();
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
    function _isReadOnlySession() { return false; }
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
  const invalidHeader = document.querySelector('[data-target="cross-profile-target"]');
  const invalidDragover = new DragEvent('dragover', {
    bubbles: true,
    cancelable: true,
    dataTransfer: protectedDragoverData,
  });
  observed.invalidDragoverDefaultPrevented = !invalidHeader.dispatchEvent(invalidDragover);
  observed.invalidDragoverEffect = protectedDragoverData.dropEffect;
  observed.invalidDragoverClass = invalidHeader.classList.contains('drag-over');
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
      "invalidDragoverDefaultPrevented": False,
      "invalidDragoverEffect": "none",
      "invalidDragoverClass": False,
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
            "body": {"session_id": "to-unassigned", "project_id": None},
        },
    ]
    assert observed["renderCount"] == 2
    assert observed["toasts"] == [
        "Moved to Compatible",
        "Move failed: failed-target blocked",
        "Removed from project",
    ]
    assert next(item for item in observed["sessions"] if item["session_id"] == "compatible")["project_id"] == "target"
    assert next(item for item in observed["sessions"] if item["session_id"] == "failed")["project_id"] == "source"
    assert next(item for item in observed["sessions"] if item["session_id"] == "fallback")["project_id"] == "source"


def test_grouped_drag_playwright_uses_native_drag_and_drop_for_valid_target():
    playwright_factory = _require_playwright()
    binding_start = SESSIONS_JS.index("const isGroupedDragControl=")
    binding_end = SESSIONS_JS.index("const dragHandle=", binding_start)
    source_binding = SESSIONS_JS[binding_start:binding_end]
    helpers = "\n".join(
        [_extract_js_line(SESSIONS_JS, prefix) for prefix in (
            "const SESSION_PROJECT_DRAG_MIME=",
            "const SESSION_PROJECT_DRAG_TEXT_PREFIX=",
            "let _activeSidebarProjectDragSessionId=",
        )]
        + [
            _extract_js_function(SESSIONS_JS, name)
            for name in (
                "_profileNameIsRoot",
                "_profileNameIsRootAlias",
                "_normalizedProfileEquivalent",
                "_profileNamesEquivalent",
                "_projectMoveEligibility",
                "_setSessionProjectDragData",
                "_clearSessionProjectDragData",
                "_sessionProjectDragSid",
                "_isSessionProjectMoveDrag",
                "_handleGroupedProjectDrop",
                "_bindGroupedProjectDropTarget",
                "_moveSessionToProject",
            )
        ]
    )
    fixture = """
const _profilesCache = {profiles:[{name:'alpha',is_default:false},{name:'beta',is_default:false}]};
window._profilesCacheFreshAt = Date.now();
const _allSessions = [{session_id:'native-source',project_id:'source',profile:'alpha'}];
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
function _isReadOnlySession() { return false; }
""" + helpers + """
const sourceSession = {session_id:'native-source'};
const source = document.createElement('div');
source.id = 'native-source';
source.className = 'session-title-row';
source.draggable = true;
source.textContent = 'source';
const s = sourceSession;
const el = source;
const titleRow = source;
__SOURCE_BINDING__
const valid = document.createElement('div');
valid.className = 'project-chip';
valid.id = 'native-valid';
valid.textContent = 'valid';
valid.style.cssText = 'display:block;width:120px;height:40px;';
const unassigned = document.createElement('div');
unassigned.className = 'session-project-group-header';
unassigned.id = 'native-unassigned';
unassigned.textContent = 'unassigned';
unassigned.style.cssText = 'display:block;width:120px;height:40px;';
const same = document.createElement('div');
same.className = 'project-chip';
same.id = 'native-same';
same.textContent = 'same';
same.style.cssText = 'display:block;width:120px;height:40px;';
const invalid = document.createElement('div');
invalid.className = 'project-chip';
invalid.id = 'native-invalid';
invalid.textContent = 'invalid';
invalid.style.cssText = 'display:block;width:120px;height:40px;';
const failed = document.createElement('div');
failed.className = 'project-chip';
failed.id = 'native-failed';
failed.textContent = 'failed';
failed.style.cssText = 'display:block;width:120px;height:40px;';
document.body.append(source, valid, unassigned, same, invalid, failed);
_bindGroupedProjectDropTarget(valid, {project_id:'target',profile:'alpha'}, 'Target');
_bindGroupedProjectDropTarget(unassigned, null, 'Unassigned');
_bindGroupedProjectDropTarget(same, null, 'Unassigned');
_bindGroupedProjectDropTarget(invalid, {project_id:'other',profile:'beta'}, 'Other');
_bindGroupedProjectDropTarget(failed, {project_id:'failed-target',profile:'alpha'}, 'Failed');
window.nativeDragState = {valid, unassigned, same, invalid, failed, source};
"""
    fixture = fixture.replace("__SOURCE_BINDING__", source_binding)
    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(content=fixture)
        page.drag_and_drop("#native-source", "#native-valid")
        page.drag_and_drop("#native-source", "#native-unassigned")
        page.drag_and_drop("#native-source", "#native-same")
        page.drag_and_drop("#native-source", "#native-invalid")
        page.drag_and_drop("#native-source", "#native-failed")
        observed = page.evaluate("""
() => ({
  apiCalls,
  renderCount,
  toasts,
  projectId: _allSessions[0].project_id,
  validClass: nativeDragState.valid.classList.contains('drag-over'),
  unassignedClass: nativeDragState.unassigned.classList.contains('drag-over'),
  sameClass: nativeDragState.same.classList.contains('drag-over'),
  invalidClass: nativeDragState.invalid.classList.contains('drag-over'),
  failedClass: nativeDragState.failed.classList.contains('drag-over'),
  validTargetIsProjectChip: nativeDragState.valid.classList.contains('project-chip'),
})
""")
        browser.close()
    assert observed["apiCalls"] == [
        {"url":"/api/session/move","body":{"session_id":"native-source","project_id":"target"}},
        {"url":"/api/session/move","body":{"session_id":"native-source","project_id":None}},
        {"url":"/api/session/move","body":{"session_id":"native-source","project_id":"failed-target"}},
    ], observed
    assert observed["renderCount"] == 2
    assert observed["toasts"] == ["Moved to Target", "Removed from project", "Move failed: failed-target blocked"]
    assert observed["projectId"] is None
    assert observed["validClass"] is False
    assert observed["unassignedClass"] is False
    assert observed["sameClass"] is False
    assert observed["invalidClass"] is False
    assert observed["failedClass"] is False
    assert observed["validTargetIsProjectChip"] is True
