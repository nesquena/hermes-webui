"""Production-composed regressions for the scoped sidebar lineage index."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(body: str):
    result = subprocess.run(
        [NODE],
        input=body,
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def _harness(body: str) -> str:
    js = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const start = src.search(new RegExp('function\\\\s+' + name + '\\\\s*\\\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start), depth = 1; i++;
  while (depth && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
eval(extractFunc('_sessionProfileScope'));
eval(extractFunc('_sidebarLineageSourceBucket'));
eval(extractFunc('_isReadOnlySession'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_authoritativeLineageTipId'));
eval(extractFunc('_buildSidebarLineageIndex'));
eval(extractFunc('_createSidebarRuntimeContext'));
eval(extractFunc('_sidebarActiveSessionIdentityKey'));
eval(extractFunc('_sidebarIdentityMatchesActiveSession'));
eval(extractFunc('_sidebarSessionMatchesActiveSession'));
eval(extractFunc('_sidebarRuntimeIdentityKey'));
eval(extractFunc('_sidebarRuntimeKey'));
eval(extractFunc('_sidebarRuntimeRowForSid'));
eval(extractFunc('_sidebarStateKey'));
eval(extractFunc('_migrateSidebarStateEntry'));
eval(extractFunc('_sessionLineageContainsSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
{body}
"""


def test_reported_fork_and_delegate_rows_stay_stable_across_rebuilds():
    result = _run_node(_harness("""
const root = {session_id:'root', title:'Renamed root', profile_scope:'work', project_id:'projA'};
const delegated = {session_id:'delegate', title:'Delegated child', profile_scope:'work',
  relationship_type:'child_session', read_only:true, parent_session_id:'root'};
const fork = {session_id:'fork', title:'Writable fork', profile_scope:'work',
  session_source:'fork', relationship_type:'child_session', parent_session_id:'root', project_id:null};
function render(rows, refs) {
  const index = _buildSidebarLineageIndex(rows, refs);
  return rows.map(row => ({id:row.session_id, project:index.projectFor(row),
    scope:index.scopeKey(row), linkable:index.isLinkable(row)}));
}
const narrow = render([root, delegated, fork], []);
const restored = render([{...root, title:'Renamed root'}, delegated, fork], [root]);
console.log(JSON.stringify({narrow, restored}));
"""))
    assert result["narrow"] == result["restored"]
    assert result["narrow"][1]["project"] == "projA"
    assert result["narrow"][2]["project"] is None
    assert result["narrow"][1]["linkable"]


def test_four_gate_defects_are_closed_by_production_index_and_collapse():
    result = _run_node(_harness("""
const parent = {session_id:'same', profile_scope:'p', project_id:'A'};
const fork = {session_id:'fork', profile_scope:'p', session_source:'fork',
  relationship_type:'child_session', parent_session_id:'same', project_id:null};
const delegate = {session_id:'delegate', profile_scope:'p', relationship_type:'child_session',
  read_only:true, parent_session_id:'same'};
const profileB = {session_id:'same', profile_scope:'other', project_id:'B'};
const tipA = {session_id:'tip-a', profile_scope:'p', project_id:'A', _lineage_root_id:'same'};
const tipB = {session_id:'tip-b', profile_scope:'other', project_id:'B', _lineage_root_id:'same'};
const index = _buildSidebarLineageIndex([parent, fork, delegate, profileB, tipA, tipB], []);
const collapsed = _collapseSessionLineageForSidebar([tipA, tipB], index);
console.log(JSON.stringify({fork:index.ownership(fork), delegate:index.ownership(delegate),
  collapsed:collapsed.map(row=>row.session_id), scopes:[index.scopeKey(tipA), index.scopeKey(tipB)]}));
"""))
    assert result["fork"]["status"] == "resolved_null"
    assert result["delegate"]["project"] == "A"
    assert result["collapsed"] == ["tip-a", "tip-b"]
    assert result["scopes"][0] != result["scopes"][1]


@pytest.mark.parametrize("size", [100, 500, 1000, 2000])
def test_nested_lineage_resolution_is_linear(size):
    result = _run_node(_harness(f"""
const rows = [{{session_id:'root', profile_scope:'p', project_id:'proj'}}];
for(let i=1;i<{size};i++) rows.push({{session_id:'n'+i, profile_scope:'p',
  relationship_type:'child_session', read_only:true, parent_session_id:i===1?'root':'n'+(i-1)}});
const index = _buildSidebarLineageIndex(rows, []);
const last = rows[rows.length-1];
console.log(JSON.stringify({{project:index.projectFor(last), visits:index.stats.nodeVisits,
  edges:index.stats.edgeVisits, linkable:index.isLinkable(last)}}));
"""))
    assert result["project"] == "proj"
    assert result["linkable"]
    assert result["visits"] <= size
    assert result["edges"] <= size


def test_snapshot_lineage_index_contract_and_invalid_identity_are_distinct():
    result = _run_node(_harness("""
const root = {session_id:'root', profile_scope:'p', project_id:null};
const missing = {session_id:'missing', profile_scope:'p', relationship_type:'child_session',
  read_only:true, parent_session_id:'unknown'};
const index = _buildSidebarLineageIndex([root, missing], []);
console.log(JSON.stringify({root:index.ownership(root), missing:index.ownership(missing),
  rootKey:index.identityKey(root,'same'), missingKey:index.identityKey(missing,'same')}));
"""))
    assert result["root"]["status"] == "resolved_null"
    assert result["missing"]["status"] == "missing"
    assert result["rootKey"] != result["missingKey"]


def test_same_profile_project_duplicates_fail_closed_before_attachment():
    result = _run_node(_harness("""
const parentA = {session_id:'parent', profile_scope:'work', project_id:'projA'};
const parentB = {session_id:'parent', profile_scope:'work', project_id:'projB'};
const delegate = {session_id:'delegate', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'parent'};
const tipA = {session_id:'tipA', profile_scope:'work', project_id:'projA', _lineage_root_id:'same'};
const tipB = {session_id:'tipB', profile_scope:'work', project_id:'projB', _lineage_root_id:'same'};
const index = _buildSidebarLineageIndex([parentA, parentB, delegate, tipA, tipB], []);
const collapsed = _collapseSessionLineageForSidebar([tipA, tipB], index);
console.log(JSON.stringify({delegate:index.ownership(delegate),
  project:index.projectFor(delegate)===undefined?'invalid':index.projectFor(delegate),
  collapsed:collapsed.map(row=>row.session_id)}));
"""))
    assert result["delegate"]["status"] == "ambiguous"
    assert result["project"] == "invalid"
    assert result["collapsed"] == ["tipA", "tipB"]


def test_duplicate_delegate_ids_keep_distinct_parent_project_context():
    result = _run_node(_harness("""
const parentA = {session_id:'parent-a', profile_scope:'work', project_id:'projA'};
const parentB = {session_id:'parent-b', profile_scope:'work', project_id:'projB'};
const childA = {session_id:'child', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'parent-a'};
const childB = {session_id:'child', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'parent-b'};
global._allSessions = [parentA, parentB, childA, childB];
const index = _buildSidebarLineageIndex(global._allSessions, []);
const context = _createSidebarRuntimeContext(global._allSessions, []);
console.log(JSON.stringify({
  projectA:index.projectFor(childA),
  projectB:index.projectFor(childB),
  runtimeA:_sidebarRuntimeIdentityKey(childA,null,context),
  runtimeB:_sidebarRuntimeIdentityKey(childB,null,context),
}));
"""))
    assert result["projectA"] == "projA"
    assert result["projectB"] == "projB"
    assert result["runtimeA"] != result["runtimeB"]


def test_active_session_matching_uses_scoped_lineage_identity():
    result = _run_node(_harness("""
global.S = {session:{session_id:'same', profile_scope:'work', project_id:'projA'}};
const rowA = {session_id:'same', profile_scope:'work', project_id:'projA'};
const rowB = {session_id:'same', profile_scope:'work', project_id:'projB'};
const index = _buildSidebarLineageIndex([rowA, rowB], []);
console.log(JSON.stringify({same:_sessionLineageContainsSession(rowA, 'same', index), other:_sessionLineageContainsSession(rowB, 'same', index)}));
"""))
    assert result == {"same": True, "other": False}


def test_runtime_state_keys_keep_duplicate_session_ids_scoped():
    result = _run_node(_harness("""
const rowA = {session_id:'same', profile_scope:'work', project_id:'projA'};
const rowB = {session_id:'same', profile_scope:'work', project_id:'projB'};
global._allSessions = [rowA, rowB];
global.S = {session: rowA};
const context = _createSidebarRuntimeContext(global._allSessions, []);
console.log(JSON.stringify({
  a:_sidebarRuntimeIdentityKey(rowA,null,context),
  b:_sidebarRuntimeIdentityKey(rowB,null,context),
  active:_sidebarRuntimeIdentityKey(rowA,null,context),
}));
"""))
    assert result["a"] != result["b"]
    assert result["active"] == result["a"]


def test_runtime_context_precomputes_many_keys_without_rebuilding_the_lineage_index():
    result = _run_node(_harness("""
const rows = [];
for (let i = 0; i < 2400; i++) rows.push({session_id:i % 2 ? 'same' : 'sid-'+i,
  profile_scope:i % 2 ? 'profile-'+(i % 3) : 'work', project_id:'project-'+(i % 5)});
const context = _createSidebarRuntimeContext(rows, []);
let builds = 0;
const build = _buildSidebarLineageIndex;
global._buildSidebarLineageIndex = (...args) => { builds++; return build(...args); };
const keys = rows.map(row => _sidebarRuntimeKey(row, null, context));
console.log(JSON.stringify({builds, keys:keys.length, distinct:new Set(keys).size, same:keys[1] !== keys[3]}));
"""))
    assert result["builds"] == 0
    assert result["keys"] == 2400
    assert result["distinct"] > 1200
    assert result["same"] is True


def test_list_apply_reuses_one_runtime_index_for_merge_and_lineage_prune():
    result = _run_node(_harness("""
let buildCount = 0;
const instrumentedIndex = extractFunc('_buildSidebarLineageIndex').replace(
  '{', '{ buildCount++;', 1);
eval(instrumentedIndex);
eval(extractFunc('_createSidebarRuntimeContext'));
eval(extractFunc('_pruneLineageReportCacheToVisibleSessions'));
eval(extractFunc('_applySessionListPayload'));
global.window = {_showCliSessions:false};
global._allSessions = [{session_id:'local', profile_scope:'profile-a', project_id:'project-a',
  is_streaming:true, message_count:1, pending_user_message:'local turn'}];
global._sidebarReferenceSessions = [{session_id:'archived-parent', profile_scope:'profile-a',
  project_id:'project-a', archived:true}];
global._optimisticallyRemovedSessionIds = new Set();
global._allSessionsScope = null;
global._allProjects = [];
global._sessionListLoadError = null;
global._sessionListHasLoadedOnce = false;
global._sessionListFirstRenderAnimated = true;
global._sessionListSkeletonActive = true;
global._sessionListRefreshAnimationPending = false;
global._lastSessionListRenderSig = null;
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global._showAllProfiles = true;
global._sessionSourceFilter = null;
global._renamingSid = null;
global._sessionActionMenu = null;
global._otherProfileCount = 0;
global._archivedWebuiCount = 0;
global._archivedCliCount = 0;
global._serverWebuiSessionCount = null;
global._serverCliSessionCount = null;
global._serverTimeDelta = 0;
global._serverTz = null;
global._cronPollGeneration = 0;
global._sessionStreamingById = new Map();
global._sessionListSnapshotById = new Map();
global._sessionListSourceById = new Map();
global._sessionObservedStreaming = {};
global.S = {activeProfile:'profile-a', session:null};
let pruneCalls = [];
global._pruneLineageReportCacheToVisibleSessions = (rows,index,context) => {
  pruneCalls.push({rows,index,context});
};
global._reconcileActiveSessionIdleStateFromList = () => false;
global._mergeOptimisticFirstTurnSessions = rows => rows;
global._syncSessionAttentionSoundState = () => {};
global._recordSessionProfileCount = () => {};
global._markPollingCompletionUnreadTransitions = rows => {
  if (rows.some(row => row.session_id === 'cron-running' && row.cron_running !== true)) {
    throw new Error('cron_running signal was changed before polling completion handling');
  }
};
global._isSessionEffectivelyStreaming = row => Boolean(row && (row.is_streaming || row.cron_running));
global.startStreamingPoll = () => {};
global.stopStreamingPoll = () => {};
global.ensureSessionTimeRefreshPoll = () => {};
global.ensureActiveSessionExternalRefreshPoll = () => {};
global.ensureSessionEventsSSE = () => {};
global.animateNextSessionListRefresh = () => {};
global.renderSessionListFromCache = () => {};
global._sessionListRenderSignature = () => '';
global._sessionListExcludeHiddenEnabled = () => true;
global._requestedSessionSidebarSource = () => 'webui';
global._recordSessionProfileCount = () => {};
_applySessionListPayload({
  active_profile:'profile-a',
  sessions:[
    {session_id:'incoming-a', profile_scope:'profile-a', project_id:'project-a', message_count:2},
    {session_id:'same', profile_scope:'profile-a', project_id:'project-a', message_count:3},
    {session_id:'same', profile_scope:'profile-b', project_id:'project-b', message_count:4},
    {session_id:'cron-running', profile_scope:'profile-a', project_id:null, cron_running:true, is_streaming:true},
  ],
  sidebar_reference_sessions:[{session_id:'archived-parent', profile_scope:'profile-a', project_id:'project-a', archived:true}],
}, {projects:[]});
const context = pruneCalls[0].context;
console.log(JSON.stringify({buildCount, pruneCalls:pruneCalls.length,
  sameKeys:context.key(context.rows.find(row => row.session_id === 'same'), 'same') !==
    context.key(context.rows.find(row => row.session_id === 'same' && row.profile_scope === 'profile-b'), 'same'),
  sameIndex:pruneCalls[0].index === context.index,
  cronSource:global._allSessions.find(row => row.session_id === 'cron-running').cron_running}));
"""))
    assert result == {"buildCount": 1, "pruneCalls": 1, "sameKeys": True, "sameIndex": True, "cronSource": True}


def test_viewed_and_completion_state_keep_duplicate_session_ids_scoped():
    result = _run_node(_harness("""
const store = {};
global.localStorage = {
  getItem:key => Object.prototype.hasOwnProperty.call(store,key) ? store[key] : null,
  setItem:(key,value) => { store[key] = String(value); },
};
let _sessionViewedCounts = null;
let _sessionCompletionUnread = null;
const SESSION_VIEWED_COUNTS_KEY = 'session-viewed-counts';
const SESSION_COMPLETION_UNREAD_KEY = 'session-completion-unread';
eval(extractFunc('_getSessionViewedCounts'));
eval(extractFunc('_saveSessionViewedCounts'));
eval(extractFunc('_setSessionViewedCount'));
eval(extractFunc('_getSessionCompletionUnread'));
eval(extractFunc('_saveSessionCompletionUnread'));
eval(extractFunc('_clearSessionCompletionUnread'));
eval(extractFunc('_markSessionCompletionUnread'));
eval(extractFunc('_hasSessionCompletionUnread'));
const rowA = {session_id:'same', profile_scope:'work', project_id:'projA'};
const rowB = {session_id:'same', profile_scope:'work', project_id:'projB'};
global._allSessions = [rowA, rowB];
global.S = {session: rowA};
const context = _createSidebarRuntimeContext(global._allSessions, []);
_setSessionViewedCount('same', 1, rowA, context);
_setSessionViewedCount('same', 2, rowB, context);
_markSessionCompletionUnread('same', 3, null, rowA, context);
_markSessionCompletionUnread('same', 4, null, rowB, context);
console.log(JSON.stringify({
  viewed:Object.keys(_getSessionViewedCounts()),
  unread:Object.keys(_getSessionCompletionUnread()),
  values:Object.values(_getSessionCompletionUnread()).map(item=>item.message_count).sort(),
}));
"""))
    assert len(result["viewed"]) == 2
    assert len(result["unread"]) == 2
    assert result["values"] == [3, 4]


def test_compression_parent_lookup_stays_within_profile_scope():
    result = _run_node(_harness("""
const parentA = {session_id:'parent', profile_scope:'A', project_id:'projA',
  pre_compression_snapshot:true, updated_at:10};
const tipA = {session_id:'tipA', profile_scope:'A', parent_session_id:'parent',
  project_id:'projA', updated_at:20};
const parentB = {session_id:'parent', profile_scope:'B', project_id:'projB',
  pre_compression_snapshot:true, updated_at:30};
const rows = [parentA, tipA, parentB];
const index = _buildSidebarLineageIndex(rows, []);
const collapsed = _collapseSessionLineageForSidebar(rows, index);
console.log(JSON.stringify(collapsed.map(row => ({
  id: row.session_id, profile: row.profile_scope, count: row._lineage_collapsed_count || 1
}))));
"""))
    assert result == [
        {"id": "tipA", "profile": "A", "count": 2},
        {"id": "parent", "profile": "B", "count": 1},
    ]


def test_attachment_accepts_the_collapsed_index_identity_without_rescoping():
    result = _run_node(_harness("""
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const root = {session_id:'root', profile_scope:'work', project_id:'projA',
  _lineage_root_id:'root', _lineage_tip_id:'tip'};
const tip = {session_id:'tip', profile_scope:'work', project_id:'projA',
  _lineage_root_id:'root', _lineage_tip_id:'tip'};
const child = {session_id:'child', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'tip', _parent_lineage_root_id:'root'};
const raw = [root, tip, child];
const index = _buildSidebarLineageIndex(raw, []);
const collapsed = _collapseSessionLineageForSidebar(raw, index);
const attached = _attachChildSessionsToSidebarRows(collapsed, raw, [], undefined, index);
console.log(JSON.stringify(attached.map(row => ({
  id: row.session_id, children: (row._child_sessions || []).map(item => item.session_id)
}))));
"""))
    assert result == [{"id": "tip", "children": ["child"]}]
