import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static/ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE], input=source, cwd=ROOT, capture_output=True,
        encoding="utf-8", text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def test_activity_ui_uses_dedicated_endpoint_and_known_session_labels():
    assert "/api/activity/active-runs" in UI_JS
    start = UI_JS.index("refreshActiveRunVisibility")
    assert "/health" not in UI_JS[start:start + 1800]
    assert "_allSessions" in UI_JS and "loadSession(run.session_id)" in UI_JS
    assert "_activeRunSnapshotRequestSeq" in UI_JS
    assert "_activeRunSnapshotInflight" in UI_JS
    assert "_activeRunSnapshotRefreshQueued" in UI_JS
    assert "if (_activeRunSnapshotInflight)" in UI_JS
    assert "requestSeq !== _activeRunSnapshotRequestSeq" in UI_JS
    assert "ACTIVE_RUN_SNAPSHOT_STALE_MS" in UI_JS
    assert "_activeRunSnapshotFreshAt = 0;" in UI_JS
    assert "activeRunPill" in INDEX_HTML and "activeRunTray" in INDEX_HTML


def test_active_run_lights_only_existing_sidebar_ring():
    assert "function _isSessionRingStreaming(s)" in SESSIONS_JS
    shared = SESSIONS_JS[
        SESSIONS_JS.index("function _isSessionEffectivelyStreaming(s)"):SESSIONS_JS.index(
            "function _isSessionRingStreaming(s)"
        )
    ]
    assert "_activeRunSessionIds" not in shared
    assert "_activeRunSessionIds.has(s.session_id)" in SESSIONS_JS
    assert "const ownRingStreaming=_isSessionRingStreaming(s);" in SESSIONS_JS
    assert "session-state-indicator" in SESSIONS_JS
    assert "_reconcileActiveSessionIdleStateFromList" in SESSIONS_JS
    assert "_activeRunSessionIds.clear()" in UI_JS


def test_active_run_only_row_renders_ring_without_lifecycle_state_or_unread():
    source = f"""
const src = {SESSIONS_JS!r};
function extract(name) {{
  const start = src.indexOf('function ' + name + '(');
  let i = src.indexOf('{{', start), depth = 1;
  while (depth && ++i < src.length) {{ if (src[i] === '{{') depth++; if (src[i] === '}}') depth--; }}
  return src.slice(start, i + 1);
}}
const renderStart = src.indexOf('function _renderOneSession(s, isPinnedGroup=false){{');
const renderClassEnd = src.indexOf("    const swipeReturnOffset=", renderStart);
const renderPrefix = src.slice(renderStart, renderClassEnd) + '    return el;\\n  }}';
const remembered = new Map(); let completionUnreadCalls = 0;
global.S = {{session:null, busy:false}};
global.activeSidForSidebar = null;
global._activeRunSessionIds = new Set(['active-run-only']);
global._sessionStreamingById = remembered;
global._sessionListSnapshotById = new Map();
global._sessionListSourceById = new Map();
global._allSessionsScope = {{}};
global._sessionLineageContainsSession = () => false;
global._hasUnreadForSession = () => false;
global._sessionAttentionState = () => null;
global._isReadOnlySession = () => false;
global._rememberRenderedSessionSnapshot = () => {{}};
global._rememberObservedStreamingSession = () => {{}};
global._rememberSessionListSource = () => {{}};
global._getSessionObservedStreaming = () => ({{}});
global._isSessionActivelyViewedForList = () => false;
global._setSessionViewedCount = () => {{}};
global._forgetObservedStreamingSession = () => {{}};
global._markSessionCompletionUnread = () => {{ completionUnreadCalls++; }};
global.document = {{createElement: () => ({{className:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}, dataset:{{}}, addEventListener(){{}}}})}};
eval(extract('_isSessionLocallyStreaming'));
eval(extract('_hasPendingUserMessageSignal'));
eval(extract('_isSessionEffectivelyStreaming'));
eval(extract('_isSessionRingStreaming'));
eval(extract('_rememberRenderedStreamingState'));
eval(extract('_sessionRunningSortRank'));
eval(extract('_markPollingCompletionUnreadTransitions'));
eval(renderPrefix);
const row = {{session_id:'active-run-only', message_count:0}};
const active = _renderOneSession(row);
const activeState = {{
  ring: active.className.includes(' streaming'),
  shared: _isSessionEffectivelyStreaming(row),
  rank: _sessionRunningSortRank(row),
  remembered: remembered.get(row.session_id) === true,
}};
_markPollingCompletionUnreadTransitions([row]);
_activeRunSessionIds.clear();
const idle = _renderOneSession(row);
_markPollingCompletionUnreadTransitions([row]);
console.log(JSON.stringify({{active:activeState, idle:{{
  ring: idle.className.includes(' streaming'),
  shared: _isSessionEffectivelyStreaming(row),
  rank: _sessionRunningSortRank(row),
  remembered: remembered.get(row.session_id) === true,
  completionUnreadCalls,
}}}}));
"""
    result = _run_node(source)
    assert result["active"] == {"ring": True, "shared": False, "rank": 0, "remembered": False}
    assert result["idle"] == {
        "ring": False, "shared": False, "rank": 0, "remembered": False,
        "completionUnreadCalls": 0,
    }


def test_active_run_only_child_and_fork_child_render_rings_without_lifecycle_state():
    source = f"""
const src = {SESSIONS_JS!r};
function extract(name) {{
  const start = src.indexOf('function ' + name + '(');
  let i = src.indexOf('{{', start), depth = 1;
  while (depth && ++i < src.length) {{ if (src[i] === '{{') depth++; if (src[i] === '}}') depth--; }}
  return src.slice(start, i + 1);
}}
function extractBlock(start) {{
  let i = src.indexOf('{{', start), depth = 1;
  while (depth && ++i < src.length) {{ if (src[i] === '{{') depth++; if (src[i] === '}}') depth--; }}
  return src.slice(start, i + 1);
}}
const forkLoopStart = src.indexOf('      for(const child of sortedChildren){{', src.indexOf("const childList=document.createElement('div');"));
const forkLoop = extractBlock(forkLoopStart);
global.S = {{session:null, busy:false}};
global._activeRunSessionIds = new Set(['active-child', 'active-fork']);
global._showArchived = false;
global._hasUnreadForSession = () => false;
global._isExternalSession = () => false;
global._isMessagingSession = () => false;
global._isReadOnlySession = () => true;
global._sessionTimestampMs = () => 0;
global._sessionDisplayTitle = (s) => s.title || 'Untitled';
global._formatRelativeSessionTime = () => 'now';
global._sessionAttentionState = () => null;
global._sessionStateTooltip = () => '';
global._sessionLineageContainsSession = () => false;
global.activeSidForSidebar = null;
global._sessionSelectMode = false;
function element() {{
  return {{className:'', children:[], dataset:{{}}, style:{{}}, classList:{{add(){{}}, remove(){{}}, toggle(){{}}}}, appendChild(child){{this.children.push(child);}}, append(...children){{this.children.push(...children);}}, addEventListener(){{}}, setAttribute(){{}}}};
}}
global.document = {{createElement: element}};
eval(extract('_isSessionLocallyStreaming'));
eval(extract('_hasPendingUserMessageSignal'));
eval(extract('_isSessionEffectivelyStreaming'));
eval(extract('_isSessionRingStreaming'));
eval(extract('_sessionRunningSortRank'));
eval(extract('_isChildSession'));
eval(extract('_isForkWithResolvableParent'));
eval(extract('_sidebarLineageKeyForRow'));
eval(extract('_attachChildSessionsToSidebarRows'));
const child = {{session_id:'active-child', parent_session_id:'parent', relationship_type:'child_session', title:'Child'}};
const parent = {{session_id:'parent', title:'Parent'}};
const attached = _attachChildSessionsToSidebarRows([parent], [parent, child]);
const attachedParent = attached[0];
const sortedChildren = [{{session_id:'active-fork', session_source:'fork', title:'Fork child'}}];
const childList = document.createElement('div');
const childLabelFor = (child) => child.title;
const openChildSession = async () => {{}};
const installForkChildSwipe = () => {{}};
const _buildSessionRenameStarter = () => () => {{}};
eval('(function(){{' + forkLoop + '}})()');
const forkRow = childList.children[0];
console.log(JSON.stringify({{
  childRing: attachedParent._child_session_streaming === true,
  childShared: _isSessionEffectivelyStreaming(child),
  childRank: _sessionRunningSortRank(child),
  childFastPoll: [child].some(_isSessionEffectivelyStreaming),
  forkRing: forkRow.className.includes(' streaming') && forkRow.children[1].className.includes(' is-streaming'),
  forkShared: _isSessionEffectivelyStreaming(sortedChildren[0]),
  forkRank: _sessionRunningSortRank(sortedChildren[0]),
  forkFastPoll: sortedChildren.some(_isSessionEffectivelyStreaming),
}}));
"""
    assert _run_node(source) == {
        "childRing": True, "childShared": False, "childRank": 0, "childFastPoll": False,
        "forkRing": True, "forkShared": False, "forkRank": 0, "forkFastPoll": False,
    }
