"""Regression tests for #6027 — sidebar lineage instability with delegated /
subagent children and forks.

#5699 closed the date-bucket-jump vector (it removed the ``_sidebar_activity_at``
bump from ``bubbleSidebarState``). It left two coupled vectors live, both
reproduced here against the real ``static/sessions.js`` regions:

D/E — "sub-agents blink out".  ``_sidebarRowHasVisibleMessages`` only kept a
  zero-message delegate child alive for the ACTIVE parent (#5306).  A child or
  fork whose parent is a *visible row in this render* but which transiently
  reports ``message_count === 0`` between ``/api/sessions`` polls was dropped by
  the predicate, so it never reached ``_attachChildSessionsToSidebarRows``: the
  parent's ``_child_sessions`` / ``_child_session_count`` badge blinked off and
  back on, changing the parent row's height and reflowing every row beneath it.

F — "forks detach from root / rows reflow".  ``_cross_surface_child_session`` is
  only set when parent and child sources DIFFER
  (``api/agent_sessions.py``), so a same-source delegation chain (subagent ->
  subagent) carries no marker and is promoted to a top-level orphan row.
  ``_sessionRunningSortRank`` then pinned that row to the very top for as long
  as it streamed and dropped it on completion — the child's activity changed its
  POSITION instead of surfacing through the existing streaming indicator.

The helpers under test are the real regions extracted from static/sessions.js and
run under node, matching tests/test_5306_subagent_sidebar_flicker.py.
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

# Loaded once; every scenario is appended as plain JS.
_HARNESS = """
const src = __JS__;
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
function _isCliSession(s){ return !!(s && (s.is_cli_session || s.session_source==='cli')); }
function _isExternalSession(s){ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }
function _isMessagingSession(s){ return !!(s && s.session_source==='messaging'); }
function _hasUnreadForSession(s){ return !!(s && s.has_unread); }
global._isCliSession=_isCliSession; global._isExternalSession=_isExternalSession;
global._isMessagingSession=_isMessagingSession; global._hasUnreadForSession=_hasUnreadForSession;
global.INFLIGHT = {};
global.NO_PROJECT_FILTER = '__no_project__';
global.window = {};
global._archivedCliCount = 0; global._archivedWebuiCount = 0;
global._serverWebuiSessionCount = null; global._serverCliSessionCount = null;
global._sidebarReferenceSessions = [];
// #6027: declared so _partitionSidebarSessionRows can publish the render scope.
// A null value means "no scope for this render", which is the safe default.
global._sidebarRenderScopeSessionIds = null;
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
global._serverTimeDelta = 0;
global._serverTz = '+0000';
global.S = { session: null, busy: false, activeStreamId: null };
// NOTE: sloppy-mode direct eval hoists these declarations into the ENCLOSING
// FUNCTION scope. Keep the eval calls at script top level (a for-of body is
// fine); wrapping them in a callback would trap each declaration inside that
// callback and nothing would be visible to the scenarios below.
const HELPERS = [
  '_isSessionLocallyStreaming','_hasPendingUserMessageSignal','_isSessionEffectivelyStreaming',
  '_isChildSession','_isDelegatedSubagentRow','_sidebarProjectResolver','_sidebarRowsById',
  '_isForkWithResolvableParent','_sessionLineageKey','_sidebarLineageKeyForRow',
  '_authoritativeLineageTipId','_sessionTimestampMs','_sessionSortTimestampMs',
  '_sessionRunningSortRank','_sessionSidebarSortCompare','_collapseSessionLineageForSidebar',
  '_attachChildSessionsToSidebarRows','_sessionAttentionState','_sidebarRowHasVisibleMessages',
  '_sidebarRenderScopeHasVisibleParent','_partitionSidebarSessionRows','_scopedSidebarReferenceRows',
  '_renderSidebarRowsFromRawSessions',
];
for (const fn of HELPERS) { eval(extractFunc(fn)); }
if (typeof _partitionSidebarSessionRows !== 'function') {
  throw new Error('harness failed to load sidebar functions');
}
function render(allMatched, activeSid){
  const part = _partitionSidebarSessionRows(allMatched, activeSid);
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
  const ordered = [...rows].sort(_sessionSidebarSortCompare);
  const byId = {};
  ordered.forEach(function(r){ byId[r.session_id] = r; });
  return {
    order: ordered.map(r => r.session_id),
    raw: part.sessionsRaw.map(s => s.session_id),
    rows: byId,
  };
}
const NOW = Date.UTC(2026, 9, 17, 12, 0, 0);
const NOW_SEC = Math.round(NOW / 1000);
function webui(sid, extra){
  return Object.assign({ session_id: sid, title: 'Conversation ' + sid,
    session_source: 'webui', raw_source: 'webui', source_tag: 'webui',
    message_count: 4, updated_at: 100, last_message_at: 100 }, extra || {});
}
function subagent(sid, parentSid, extra){
  return Object.assign({ session_id: sid, title: 'Subagent Session',
    parent_session_id: parentSid, relationship_type: 'child_session',
    raw_source: 'subagent', source_tag: 'subagent', session_source: 'other',
    _cross_surface_child_session: true, message_count: 2,
    updated_at: NOW_SEC - 30, last_message_at: NOW_SEC - 30 }, extra || {});
}
"""


def run_js(body: str):
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _HARNESS.replace("__JS__", json.dumps(js)) + body
    result = subprocess.run(
        [NODE], input=source, cwd=str(REPO_ROOT),
        capture_output=True, encoding="utf-8", text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


def test_6027_delegate_child_of_visible_parent_stays_attached_across_zero_message_polls():
    """Vector D: the parent's child badge must not blink when a delegate child
    transiently reports message_count 0 between polls. Both polls must stack the
    child under the same parent row, so the parent row's height is unchanged."""
    out = run_js("""
function poll(childMsgCount){
  // 'fresh' is the ACTIVE session; the parent under test is NOT active, which is
  // exactly the gap #5306 left open.
  return render([
    webui('fresh', { message_count: 1, updated_at: NOW_SEC - 10, last_message_at: NOW_SEC - 10 }),
    webui('p2'),
    subagent('c2', 'p2', { message_count: childMsgCount }),
  ], 'fresh').rows['p2'];
}
console.log(JSON.stringify({ zero: poll(0)._child_sessions.map(c=>c.session_id),
                             caughtUp: poll(2)._child_sessions.map(c=>c.session_id) }));
""")
    assert out["zero"] == ["c2"], "delegate child of a visible parent blinked out on the zero-message poll"
    assert out["caughtUp"] == ["c2"]


def test_6027_delegate_child_child_count_badge_is_stable_across_zero_message_polls():
    """Vector D, badge half: _child_session_count drives the parent's row height,
    so it must be identical on the lagging poll and the caught-up poll."""
    out = run_js("""
function count(childMsgCount){
  return render([
    webui('fresh', { message_count: 1, updated_at: NOW_SEC - 10, last_message_at: NOW_SEC - 10 }),
    webui('p2'),
    subagent('c2', 'p2', { message_count: childMsgCount }),
  ], 'fresh').rows['p2']._child_session_count;
}
console.log(JSON.stringify({ zero: count(0), caughtUp: count(2) }));
""")
    assert out["zero"] == 1, "child-count badge blinked off on the zero-message poll"
    assert out["zero"] == out["caughtUp"]


def test_6027_fork_of_visible_parent_stays_attached_across_zero_message_polls():
    """Vector E: a fork behaves like a delegate child here — a transient zero
    message_count must not detach it from its root's child list."""
    out = run_js("""
function forkChildren(n){
  return render([
    webui('fresh', { message_count: 1, updated_at: NOW_SEC - 10, last_message_at: NOW_SEC - 10 }),
    webui('eroot'),
    { session_id: 'efork', title: 'Forked attempt', session_source: 'fork',
      parent_session_id: 'eroot', message_count: n, updated_at: 108, last_message_at: 108 },
  ], 'fresh').rows['eroot']._child_sessions.map(c=>c.session_id);
}
console.log(JSON.stringify({ zero: forkChildren(0), caughtUp: forkChildren(1) }));
""")
    assert out["zero"] == ["efork"], "fork detached from its root on the zero-message poll"
    assert out["zero"] == out["caughtUp"]


def test_6027_streaming_orphan_delegate_child_does_not_jump_to_top_of_list():
    """Vector F: an in-progress delegate child that has no visible parent row must
    surface its activity through the streaming indicator, not by taking the top
    slot. Its index must not change between streaming and settled."""
    out = run_js("""
function grandchild(streaming){
  // No _cross_surface_child_session marker: parent and child share a source,
  // which is what a subagent -> subagent delegation chain looks like.
  const extra = streaming ? { is_streaming: true, active_stream_id: 'gst' } : {};
  return render([
    webui('fresh', { message_count: 1, updated_at: NOW_SEC - 10, last_message_at: NOW_SEC - 10 }),
    webui('older', { message_count: 2, updated_at: NOW_SEC - 600, last_message_at: NOW_SEC - 600 }),
    Object.assign({ session_id: 'gc', title: 'Subagent Session', parent_session_id: 'middle',
      relationship_type: 'child_session', raw_source: 'subagent', source_tag: 'subagent',
      session_source: 'other', message_count: 2,
      updated_at: NOW_SEC - 300, last_message_at: NOW_SEC - 300 }, extra),
  ], 'fresh').order;
}
console.log(JSON.stringify({ streaming: grandchild(true), settled: grandchild(false) }));
""")
    assert out["streaming"].index("gc") == out["settled"].index("gc"), (
        "streaming delegate child changed POSITION instead of showing its "
        "streaming indicator"
    )


def test_6027_guard_zero_message_child_of_out_of_scope_parent_stays_hidden():
    """Guard the scope of the fix: a zero-message child whose parent is NOT in
    this render must still be hidden, so we do not resurrect stale empty rows.
    Driven through _partitionSidebarSessionRows (the real call site) so the
    scope the predicate consults is the one the render actually publishes."""
    out = run_js("""
// The child 'c9' is in the payload, but its parent 'filtered_parent' is not.
const part = _partitionSidebarSessionRows([
  webui('lonely', { message_count: 0 }),
  subagent('c9', 'filtered_parent', { message_count: 0 }),
], null);
console.log(JSON.stringify({
  raw: part.sessionsRaw.map(s=>s.session_id),
  scopeClearedAfterRender: global._sidebarRenderScopeSessionIds === null,
}));
""")
    # Neither the out-of-scope child nor an unrelated empty conversation is
    # resurrected by the new scope rule.
    assert out["raw"] == []
    assert out["scopeClearedAfterRender"] is True


def test_6027_guard_running_rank_still_pins_an_ordinary_streaming_conversation():
    """Guard: the running-rank boost that pins an in-progress conversation to the
    top of the sidebar must keep working for ordinary top-level rows."""
    out = run_js("""
console.log(JSON.stringify({
  streamingTopRank: _sessionRunningSortRank(
    webui('busy', { is_streaming: true, active_stream_id: 's1' })),
  idleTopRank: _sessionRunningSortRank(webui('idle')),
  // A child that HAS its parent row visible renders as part of that row, so it
  // never reaches the top-level sort; the rank itself is unconstrained there.
}));
""")
    assert out["streamingTopRank"] == 1
    assert out["idleTopRank"] == 0


def test_6027_guard_child_of_visible_parent_is_not_promoted_to_top_level():
    """Guard the existing #5305 behaviour: a child whose parent row IS visible
    stacks under it and is never promoted to a top-level orphan."""
    out = run_js("""
const r = render([ webui('p3'), subagent('c3', 'p3') ], null);
console.log(JSON.stringify({ order: r.order, orphans: Object.keys(r.rows).filter(k=>r.rows[k]._orphan_child_session) }));
""")
    assert out["order"] == ["p3"]
    assert out["orphans"] == []