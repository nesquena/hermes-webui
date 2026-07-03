"""Regression tests for delegated-subagent sidebar bugs #5306 and #5305.

These lock two invariants for delegate/subagent child rows in the sidebar:

#5306 (flicker): while a parent WebUI session is the active/streaming session,
a linked delegate child that transiently reports ``message_count === 0`` between
``/api/sessions`` polls must NOT be dropped by the visibility predicate
(``_sidebarRowHasVisibleMessages``). Before the fix it was filtered out *before*
``_attachChildSessionsToSidebarRows`` ever saw it, so it never entered
``sessionsRaw`` — the row vanished, then reappeared on the next refresh once its
list metadata caught up (the flicker). The child must stay stacked under its
parent across re-renders even at message_count 0.

#5305 (orphan): a delegated subagent child whose WebUI parent is filtered out of
the current render (project/profile/source scope) must NOT be promoted to a
contextless top-level "Subagent Session" orphan. It follows its parent's scope
and is suppressed instead (re-stacking under the parent once that scope is
active).

The helpers under test are the *real* regions extracted from static/sessions.js
and executed under node, matching the existing style in
tests/test_session_lineage_collapse.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


# Shared preamble: extractFunc + the globals/stubs the partition + attach + render
# path reads. Kept minimal and side-effect free so each test just appends its
# scenario + a console.log.
_PREAMBLE = """
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function _isCliSession(s){{ return !!(s && (s.is_cli_session || s.session_source==='cli')); }}
function _isExternalSession(s){{ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }}
function _isMessagingSession(s){{ return !!(s && s.session_source==='messaging'); }}
function _hasUnreadForSession(s){{ return !!(s && s.has_unread); }}
global._isCliSession=_isCliSession; global._isExternalSession=_isExternalSession;
global._isMessagingSession=_isMessagingSession; global._hasUnreadForSession=_hasUnreadForSession;
global.INFLIGHT = {{}};
global.NO_PROJECT_FILTER = '__no_project__';
global.window = {{}};
global._archivedCliCount = 0; global._archivedWebuiCount = 0;
global._serverWebuiSessionCount = null; global._serverCliSessionCount = null;
global._sidebarReferenceSessions = [];
// Default idle state; tests that exercise an active/streaming parent override it.
global.S = {{ session: null, busy: false, activeStreamId: null }};
eval(extractFunc('_isSessionLocallyStreaming'));
eval(extractFunc('_hasPendingUserMessageSignal'));
eval(extractFunc('_isSessionEffectivelyStreaming'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_sessionAttentionState'));
eval(extractFunc('_sidebarRowHasVisibleMessages'));
eval(extractFunc('_partitionSidebarSessionRows'));
eval(extractFunc('_scopedSidebarReferenceRows'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
"""


def _preamble(js: str) -> str:
    return _PREAMBLE.format(js=js)


def test_5306_active_parent_delegate_child_survives_zero_message_partition():
    """#5306 flicker root cause: the visibility predicate must keep a linked
    delegate child of the ACTIVE parent even when message_count===0, so it
    reaches sessionsRaw and gets stacked under the parent instead of vanishing.
    """
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global.S = { session: { session_id: 'active_parent', message_count: 5 }, busy: true, activeStreamId: 's1' };
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const allMatched = [
  { session_id:'active_parent', title:'Parent WebUI', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:5, is_streaming:true, active_stream_id:'s1', updated_at:100, last_message_at:100 },
  { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'active_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', _parent_lineage_root_id:'active_parent', _cross_surface_child_session:true, message_count:0, updated_at:101, last_message_at:101 },
  { session_id:'unrelated_empty', title:'Unrelated empty', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:0, updated_at:50 },
];
const activeSid = 'active_parent';
const part = _partitionSidebarSessionRows(allMatched, activeSid);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
const parent = rows.find(r=>r.session_id==='active_parent') || {};
console.log(JSON.stringify({
  sessionsRaw: part.sessionsRaw.map(s=>s.session_id),
  topLevel: rows.map(r=>r.session_id),
  childCount: parent._child_session_count || 0,
  childSids: (parent._child_sessions||[]).map(c=>c.session_id),
  childPredicate: _sidebarRowHasVisibleMessages(allMatched[1], activeSid),
  unrelatedEmptyPredicate: _sidebarRowHasVisibleMessages(allMatched[2], activeSid),
}));
"""
    out = json.loads(_run_node(source))
    # The zero-message delegate child of the active parent survives partitioning.
    assert "subagent_child" in out["sessionsRaw"]
    # It is stacked UNDER the parent, not rendered as a top-level row.
    assert out["topLevel"] == ["active_parent"]
    assert out["childCount"] == 1
    assert out["childSids"] == ["subagent_child"]
    # The predicate keeps the active parent's child...
    assert out["childPredicate"] is True
    # ...but still hides a truly-empty UNRELATED session (no regression).
    assert out["unrelatedEmptyPredicate"] is False


def test_5306_child_across_two_renders_stays_present():
    """#5306 invariant across a re-render: two consecutive partitions of the
    same active-parent + zero-message delegate child must BOTH keep the child
    (no flicker between polls)."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global.S = { session: { session_id: 'active_parent', message_count: 5 }, busy: true, activeStreamId: 's1' };
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
function renderOnce(childMsgCount){
  const allMatched = [
    { session_id:'active_parent', title:'Parent WebUI', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:5, is_streaming:true, active_stream_id:'s1', updated_at:100, last_message_at:100 },
    { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'active_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', _parent_lineage_root_id:'active_parent', _cross_surface_child_session:true, message_count:childMsgCount, updated_at:101, last_message_at:101 },
  ];
  const part = _partitionSidebarSessionRows(allMatched, 'active_parent');
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
  const parent = rows.find(r=>r.session_id==='active_parent') || {};
  return (parent._child_sessions||[]).map(c=>c.session_id);
}
// Poll A: list metadata lagging, child reports 0 messages.
// Poll B: metadata caught up, child reports 2 messages.
console.log(JSON.stringify({ pollA: renderOnce(0), pollB: renderOnce(2) }));
"""
    out = json.loads(_run_node(source))
    assert out["pollA"] == ["subagent_child"], "child dropped on the zero-message poll (flicker)"
    assert out["pollB"] == ["subagent_child"], "child dropped on the caught-up poll"


def test_5306_zero_message_child_of_inactive_parent_is_still_hidden():
    """Guard the scope of the #5306 fix: the exception is for the ACTIVE parent
    only. A zero-message delegate child of some OTHER (non-active) parent stays
    hidden, so we don't resurrect stale empty children for unrelated rows."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global.S = { session: { session_id: 'active_parent', message_count: 5 }, busy: true, activeStreamId: 's1' };
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const child = { session_id:'other_child', title:'Subagent Session', parent_session_id:'inactive_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:0, updated_at:101, last_message_at:101 };
console.log(JSON.stringify({ visible: _sidebarRowHasVisibleMessages(child, 'active_parent') }));
"""
    out = json.loads(_run_node(source))
    assert out["visible"] is False


def test_5305_delegate_child_with_filtered_out_parent_is_not_orphaned():
    """#5305: a subagent child whose WebUI parent is filtered out of the current
    render (here: project filter drops the parent, child survives) must NOT be
    promoted to a top-level orphan. It is suppressed and follows the parent."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global.S = { session: null, busy: false, activeStreamId: null };
global._activeProject = global.NO_PROJECT_FILTER;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
// Parent carries project_id (dropped by the "no project" filter); the delegate
// child has no project_id and survives the same filter.
const allMatched = [
  { session_id:'proj_parent', title:'Parent WebUI', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:5, project_id:'projX', updated_at:100, last_message_at:100 },
  { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'proj_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', _parent_lineage_root_id:'proj_parent', _cross_surface_child_session:true, message_count:3, updated_at:101, last_message_at:101 },
];
const part = _partitionSidebarSessionRows(allMatched, null);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
console.log(JSON.stringify({
  sessionsRaw: part.sessionsRaw.map(s=>s.session_id),
  topLevel: rows.map(r=>r.session_id),
  orphans: rows.filter(r=>r._orphan_child_session).map(r=>r.session_id),
}));
"""
    out = json.loads(_run_node(source))
    # Child survives the visibility/project scope into sessionsRaw (parent does not)...
    assert out["sessionsRaw"] == ["subagent_child"]
    # ...but is NOT rendered as a top-level orphan.
    assert out["topLevel"] == []
    assert out["orphans"] == []


def test_5305_missing_parent_delegate_child_is_suppressed_not_orphaned():
    """#5305 at the attach layer: a cross-surface delegate child whose parent is
    entirely absent from the render is suppressed, not orphaned."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._showArchived = false;
const collapsed = [];  // parent absent from this render
const raw = [
  { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'filtered_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', source_label:'Subagent', _parent_lineage_root_id:'filtered_parent', _cross_surface_child_session:true, message_count:2 },
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows.map(r=>({sid:r.session_id, orphan:!!r._orphan_child_session}))));
"""
    out = json.loads(_run_node(source))
    assert out == []


def test_5305_visible_parent_still_stacks_subagent_child():
    """Guard the common #5244 case still holds after the #5305 change: when the
    WebUI parent IS visible in the same render, the delegate child stacks under
    it (not suppressed, not orphaned)."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._showArchived = false;
const collapsed = [{ session_id:'webui_parent', title:'Parent WebUI conversation', raw_source:'webui', source_tag:'webui', session_source:'webui', message_count:3 }];
const raw = [
  collapsed[0],
  { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'webui_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', source_label:'Subagent', _parent_lineage_root_id:'webui_parent', _cross_surface_child_session:true, message_count:2 },
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
const parent = rows.find(r=>r.session_id==='webui_parent') || {};
console.log(JSON.stringify({
  topLevel: rows.map(r=>r.session_id),
  childSids: (parent._child_sessions||[]).map(c=>c.session_id),
}));
"""
    out = json.loads(_run_node(source))
    assert out["topLevel"] == ["webui_parent"]
    assert out["childSids"] == ["subagent_child"]


def test_5305_external_parent_child_still_orphans():
    """The #5305 change must not swallow the legitimately-external case: a WebUI
    continuation child of a messaging (external) parent still renders top-level
    when the external parent has no WebUI-owned row to stack under."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._showArchived = false;
const collapsed = [{ session_id:'telegram_parent', title:'Telegram parent', session_source:'messaging', raw_source:'telegram', source_label:'Telegram' }];
const raw = [
  collapsed[0],
  { session_id:'webui_tip', title:'Current WebUI continuation', parent_session_id:'telegram_parent', relationship_type:'child_session', parent_source:'telegram', source_label:'Telegram', session_source:'messaging', raw_source:'telegram', _cross_surface_child_session:true },
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows.map(r=>({sid:r.session_id, orphan:!!r._orphan_child_session}))));
"""
    out = json.loads(_run_node(source))
    assert out == [
        {"sid": "telegram_parent", "orphan": False},
        {"sid": "webui_tip", "orphan": True},
    ]
