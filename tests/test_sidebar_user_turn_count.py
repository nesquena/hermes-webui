"""Sidebar user-turn count (#6519).

Two layers of coverage:

* A Python gate that reads ``user_message_count`` semantics off the real
  ``Session._compute_user_message_count`` and the real lineage stitcher, to
  settle whether segment counts may be summed.
* Browserless node harnesses that execute the real ``sessions.js`` functions
  (``_collapseSessionLineageForSidebar`` and the sidebar row renderer
  ``_renderOneSession``) against stubbed globals and a minimal DOM.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.agent_sessions as agent_sessions
import api.models as models
import api.routes as routes

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
NODE = shutil.which("node")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# STEP 0 validation gate: are segment counts segment-local or cumulative?
# ---------------------------------------------------------------------------


def _user_turns(messages) -> int:
    return models.Session._compute_user_message_count(messages)


def test_compression_segment_user_message_count_semantics_are_mixed(monkeypatch):
    """A continuation's own ``user_message_count`` is not a fixed unit.

    Both sidecar shapes are live in this repo and both are covered by
    ``tests/test_session_lineage_full_transcript.py``: the stitching shape,
    where the continuation holds only its own turns, and the replay shape,
    where the continuation sidecar already carries its ancestors' transcript
    and ``_webui_sidecar_lineage_messages_for_display`` returns it unchanged
    (``api/routes.py`` short-circuits on ``_messages_start_with_visible_prefix``).

    Because a sidebar row carries no marker telling the two apart, summing
    ``user_message_count`` across ``_lineage_segments`` double-counts the
    replay shape. The tip's own count never exceeds the truth in either shape,
    which is what the sidebar renders.
    """
    # Shape A — stitching: the child sidecar holds only its own turns.
    stitch_parent = SimpleNamespace(
        session_id="stitch-parent",
        parent_session_id=None,
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "first ask", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {"role": "user", "content": "second ask", "timestamp": 3.0},
            {"role": "assistant", "content": "second answer", "timestamp": 4.0},
        ],
    )
    stitch_child = SimpleNamespace(
        session_id="stitch-child",
        parent_session_id="stitch-parent",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=[
            {"role": "user", "content": "third ask", "timestamp": 5.0},
            {"role": "assistant", "content": "third answer", "timestamp": 6.0},
        ],
    )

    # Shape B — replay: the child sidecar already carries the parent transcript.
    replay_parent = SimpleNamespace(
        session_id="replay-parent",
        parent_session_id=None,
        pre_compression_snapshot=True,
        truncation_watermark=None,
        messages=list(stitch_parent.messages),
    )
    replay_child = SimpleNamespace(
        session_id="replay-child",
        parent_session_id="replay-parent",
        pre_compression_snapshot=False,
        truncation_watermark=None,
        messages=list(stitch_parent.messages) + list(stitch_child.messages),
    )

    by_id = {
        "stitch-parent": stitch_parent,
        "replay-parent": replay_parent,
    }
    monkeypatch.setattr(routes.Session, "load", lambda sid: by_id.get(sid))

    stitch_display = routes._webui_sidecar_lineage_messages_for_display(stitch_child)
    replay_display = routes._webui_sidecar_lineage_messages_for_display(replay_child)

    # Both lineages are the same three-ask conversation.
    true_total = 3
    assert _user_turns(stitch_display) == true_total
    assert _user_turns(replay_display) == true_total

    stitch_segment_counts = (
        _user_turns(stitch_parent.messages),
        _user_turns(stitch_child.messages),
    )
    replay_segment_counts = (
        _user_turns(replay_parent.messages),
        _user_turns(replay_child.messages),
    )

    # The observed per-segment numbers that decide sum-vs-tip.
    assert stitch_segment_counts == (2, 1)
    assert replay_segment_counts == (2, 3)

    # Summing is right for one shape and wrong for the other, so it is unusable.
    assert sum(stitch_segment_counts) == true_total
    assert sum(replay_segment_counts) == 5 > true_total

    # The tip's own count never exceeds the truth in either shape.
    assert stitch_segment_counts[-1] <= true_total
    assert replay_segment_counts[-1] == true_total


# ---------------------------------------------------------------------------
# Browserless JS harnesses
# ---------------------------------------------------------------------------

requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_HARNESS_PRELUDE = """
const src = {sessions_js!r};
const i18nSrc = {i18n_js!r};
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

function makeNode(tag) {{
  const node = {{
    tagName: tag, children: [], className: '', _text: '', title: '', innerHTML: '', type: '',
    dataset: {{}},
    style: {{ setProperty(){{}}, removeProperty(){{}} }},
    classList: {{
      _s: new Set(),
      add(...c){{ c.forEach(x => this._s.add(x)); }},
      remove(...c){{ c.forEach(x => this._s.delete(x)); }},
      toggle(c, on){{ if (on) this._s.add(c); else this._s.delete(c); }},
      contains(c){{ return this._s.has(c); }},
    }},
    appendChild(c){{ this.children.push(c); return c; }},
    append(...c){{ c.forEach(x => this.children.push(x)); }},
    prepend(...c){{ c.forEach(x => this.children.unshift(x)); }},
    insertBefore(c){{ this.children.push(c); return c; }},
    addEventListener(){{}}, removeEventListener(){{}}, setAttribute(){{}}, removeAttribute(){{}},
    getAttribute(){{ return null; }}, hasAttribute(){{ return false; }}, contains(){{ return false; }},
    getBoundingClientRect(){{ return {{ height: 0, width: 0 }}; }},
    querySelector(){{ return null; }}, querySelectorAll(){{ return []; }}, closest(){{ return null; }},
    focus(){{}}, remove(){{}},
  }};
  Object.defineProperty(node, 'textContent', {{
    get(){{ return this._text; }},
    set(v){{ this._text = String(v); }},
  }});
  return node;
}}
global.document = {{
  createElement: makeNode,
  createDocumentFragment: () => makeNode('#fragment'),
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  body: makeNode('body'), documentElement: makeNode('html'),
  addEventListener(){{}}, removeEventListener(){{}},
}};
global.localStorage = {{ getItem: () => null, setItem(){{}}, removeItem(){{}} }};
global.navigator = {{ language: 'en' }};
global.window = {{ _sidebarDensity: 'detailed', innerWidth: 1280, addEventListener(){{}} }};
global.requestAnimationFrame = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => {{}};

// Real locale bundles and the real t() helper.
eval(i18nSrc);

// Globals the real _renderOneSession closes over in renderSessionListFromCache.
var S = {{ session: null }};
var activeSidForSidebar = null;
var searchQueryRaw = '';
var animateRefresh = false;
var enterAllAnimatedRows = false;
var flipBefore = null;
var _renamingSid = null;
var _sessionSelectMode = false;
var _selectedSessions = new Set();
var _showArchived = false;
var _showAllProfiles = false;
var _allProjects = [];
var _sessionSwipeReturnOffsets = new Map();
var _expandedLineageKeys = new Set();
var _expandedChildSessionKeys = new Set();
var _lineageReportInflight = new Set();
var ICONS = {{ pin: '' }};
var SESSION_LONG_PRESS_DELAY_MS = 500;
var SESSION_ARCHIVE_SWIPE_THRESHOLD_PX = 72;
var SESSION_DELETE_SWIPE_THRESHOLD_PX = 72;
var SESSION_SWIPE_CANCEL_RATIO = 1;
var committedSwipeDuration = 0;
var committedSwipeReflowDelay = 0;

function li(){{ return ''; }}
function _isSessionEffectivelyStreaming(s){{ return !!(s && (s.is_streaming || s.active_stream_id)); }}
function _hasUnreadForSession(s){{ return !!(s && s.has_unread); }}
function _sessionAttentionState(){{ return null; }}
function _rememberRenderedStreamingState(){{}}
function _rememberRenderedSessionSnapshot(){{}}
function _isReadOnlySession(s){{ return !!(s && s.read_only); }}
function _isMessagingSession(s){{ return !!(s && s.session_source === 'messaging'); }}
function _isCliSession(s){{ return !!(s && s.is_cli_session); }}
function _getChannelLabel(s){{ return (s && s.source_label) || ''; }}
function _sourceKeyForSession(s){{ return (s && s.raw_source) || 'cli'; }}
function _sessionTitleTags(){{ return []; }}
function _sessionFullTitleTooltip(raw){{ return raw; }}
function _sessionTitleForForkParent(){{ return ''; }}
function _truncatedSessionId(sid){{ return String(sid || ''); }}
function _sessionForkTooltip(label){{ return String(label || ''); }}
function _appendHighlightedText(el, text){{ el.textContent = text; }}
function _formatRelativeSessionTime(){{ return 'now'; }}
function _sessionSegmentCount(s){{ return Number((s && s._compression_segment_count) || 0); }}
function _lineageReportNeedsFetch(){{ return false; }}
function _lineageSegmentsForRender(s){{ return (s && s._lineage_segments) || []; }}
function _lineageReportCacheKey(){{ return null; }}
function _sessionLineageBadgeTooltip(label){{ return label; }}
function _sessionChildBadgeTooltip(label){{ return label; }}
function _formatSessionModelWithGateway(s){{ return (s && s.model) || ''; }}
function _sessionSearchContentPreview(){{ return ''; }}
function _sessionStateTooltip(){{ return ''; }}
function _buildSessionRenameStarter(){{ return function(){{}}; }}
function _openSidebarSession(){{ return Promise.resolve(); }}
function _openSessionActionMenu(){{}}
function closeSessionActionMenu(){{}}
function renderSessionListFromCache(){{}}
function _fetchLineageReportForRow(){{ return Promise.resolve(); }}
function _archiveSession(){{ return Promise.resolve(false); }}
function deleteSession(){{ return Promise.resolve(false); }}
function _waitForSessionMotion(){{ return Promise.resolve(); }}
function setSessionSelected(){{}}
function showToast(){{}}
function _playSessionRowsReflowFromPositions(){{}}
function _makeSessionSwipeAffordance(){{ return makeNode('div'); }}

eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sessionLineageContainsSession'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_stripAttachedFilesMarker'));
eval(extractFunc('_sessionDisplayTitle'));
eval(extractFunc('_authoritativeLineageTipId'));
eval(extractFunc('_sidebarUserTurnCount'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_renderOneSession'));

// Render one real sidebar row and return its rendered meta-line text, or null
// when the row renders no meta line at all.
function metaTextFor(row, density) {{
  window._sidebarDensity = density;
  const el = _renderOneSession(row);
  const found = (function walk(node) {{
    if (!node) return null;
    if (node.className === 'session-meta') return node;
    for (const c of node.children || []) {{ const hit = walk(c); if (hit) return hit; }}
    return null;
  }})(el);
  return found ? found.textContent : null;
}}
"""


def _harness(body: str) -> str:
    prelude = _HARNESS_PRELUDE.format(
        sessions_js=SESSIONS_JS_PATH.read_text(encoding="utf-8"),
        i18n_js=I18N_JS_PATH.read_text(encoding="utf-8"),
    )
    return prelude + "\n" + body


# --- compressed continuation fixture, mirroring the shape at
# --- tests/test_session_lineage_collapse.py:286-295 with user turns added.
_OVERLAPPING_LINEAGE = """
const sessions = [
  {session_id:'parent', title:'Duplicate Assistant Text Blocks', message_count:64, user_message_count:21, updated_at:300, last_message_at:300, pre_compression_snapshot:true, _lineage_root_id:'parent', _compression_segment_count:2},
  {session_id:'child', title:'Duplicate Assistant Text Blocks', parent_session_id:'parent', message_count:86, user_message_count:29, _lineage_user_message_count:29, updated_at:200, last_message_at:200, _lineage_root_id:'parent', _compression_segment_count:2},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
"""


@requires_node
def test_collapsed_lineage_row_carries_tip_user_turn_count():
    """The collapse stamps the lineage user-turn value beside the segment list."""
    result = json.loads(_run_node(_harness(_OVERLAPPING_LINEAGE + """
console.log(JSON.stringify({
  sid: collapsed[0].session_id,
  lineageUserTurns: collapsed[0]._lineage_user_message_count,
  collapsedCount: collapsed[0]._lineage_collapsed_count,
  segments: collapsed[0]._lineage_segments.map(seg => seg.session_id),
}));
""")))
    assert result["sid"] == "child"
    assert result["lineageUserTurns"] == 29
    # The pre-existing collapse contract is untouched.
    assert result["collapsedCount"] == 2
    assert result["segments"] == ["child", "parent"]


@requires_node
def test_overlapping_compression_segments_are_not_summed():
    """A snapshot parent and its continuation must not have their turns added."""
    result = json.loads(_run_node(_harness(_OVERLAPPING_LINEAGE + """
const segmentSum = collapsed[0]._lineage_segments
  .reduce((total, seg) => total + seg.user_message_count, 0);
console.log(JSON.stringify({
  rendered: metaTextFor(collapsed[0], 'detailed'),
  segmentSum,
  lineageUserTurns: collapsed[0]._lineage_user_message_count,
}));
""")))
    assert result["segmentSum"] == 50
    assert result["lineageUserTurns"] == 29
    assert result["rendered"] == "86 msgs · 29 from you"
    assert "50" not in result["rendered"]


@requires_node
def test_detailed_meta_line_shows_user_turns_beside_total():
    result = json.loads(_run_node(_harness(_OVERLAPPING_LINEAGE + """
console.log(JSON.stringify({rendered: metaTextFor(collapsed[0], 'detailed')}));
""")))
    assert result["rendered"] == "86 msgs · 29 from you"


@requires_node
def test_uncollapsed_direct_webui_row_renders_its_own_user_turn_count():
    """A single row with no `_lineage_segments` uses its own server-side count."""
    result = json.loads(_run_node(_harness("""
const row = {session_id:'solo', title:'Direct WebUI chat', message_count:8, user_message_count:3, updated_at:10, last_message_at:10, session_source:'webui'};
const collapsed = _collapseSessionLineageForSidebar([row]);
console.log(JSON.stringify({
  collapsedRow: collapsed[0].session_id,
  hasLineageSegments: Object.prototype.hasOwnProperty.call(collapsed[0], '_lineage_segments'),
  hasLineageUserTurns: Object.prototype.hasOwnProperty.call(collapsed[0], '_lineage_user_message_count'),
  rendered: metaTextFor(collapsed[0], 'detailed'),
}));
""")))
    # The uncollapsed path is untouched: no lineage keys are stamped.
    assert result["hasLineageSegments"] is False
    assert result["hasLineageUserTurns"] is False
    assert result["rendered"] == "8 msgs · 3 from you"


@requires_node
def test_compact_density_renders_no_meta_line_for_a_collapsed_lineage():
    """Negative space: the aggregate is on the row but compact must not show it."""
    result = json.loads(_run_node(_harness(_OVERLAPPING_LINEAGE + """
console.log(JSON.stringify({
  lineageUserTurns: collapsed[0]._lineage_user_message_count,
  compact: metaTextFor(collapsed[0], 'compact'),
}));
""")))
    assert result["lineageUserTurns"] == 29
    assert result["compact"] is None


@requires_node
def test_unusable_user_message_counts_fall_back_to_the_legacy_meta_line():
    """Boundary: 0 is a real count and renders; null/negative/non-numeric do not."""
    result = json.loads(_run_node(_harness("""
function metaFor(patch) {
  const row = Object.assign({session_id:'row', title:'Row', message_count:5, updated_at:10, last_message_at:10}, patch);
  return metaTextFor(row, 'detailed');
}
const segments = [
  {session_id:'a', title:'Row', message_count:3, user_message_count:null, updated_at:10, last_message_at:10, _lineage_root_id:'a', _compression_segment_count:1},
  {session_id:'b', title:'Row', parent_session_id:'a', message_count:5, user_message_count:'4', updated_at:20, last_message_at:20, _lineage_root_id:'a', _compression_segment_count:2},
];
const collapsed = _collapseSessionLineageForSidebar(segments);
console.log(JSON.stringify({
  absent: metaFor({}),
  nullCount: metaFor({user_message_count:null}),
  nonNumeric: metaFor({user_message_count:'4'}),
  negative: metaFor({user_message_count:-1}),
  nan: metaFor({user_message_count:Number.NaN}),
  zero: metaFor({user_message_count:0}),
  collapsedHasLineageCount: Object.prototype.hasOwnProperty.call(collapsed[0], '_lineage_user_message_count'),
  collapsedRendered: metaTextFor(collapsed[0], 'detailed'),
}));
""")))
    legacy = "5 msgs"
    assert result["absent"] == legacy
    assert result["nullCount"] == legacy
    assert result["nonNumeric"] == legacy
    assert result["negative"] == legacy
    assert result["nan"] == legacy
    assert "NaN" not in json.dumps(result)
    # Boundary value 0: a genuine count, so it renders rather than falling back.
    assert result["zero"] == "5 msgs · 0 from you"
    # No segment carries a usable value, so the collapse stamps nothing.
    assert result["collapsedHasLineageCount"] is False
    assert result["collapsedRendered"] == legacy


@requires_node
def test_compressed_continuation_lineage_renders_the_tip_count():
    """A multi-segment compression chain reports the tip's count, not a sum."""
    result = json.loads(_run_node(_harness("""
const sessions = [
  {session_id:'seg1', title:'Graphify', message_count:400, user_message_count:40, updated_at:10, last_message_at:10, _lineage_root_id:'seg1', _compression_segment_count:1, pre_compression_snapshot:true},
  {session_id:'seg2', title:'Graphify', parent_session_id:'seg1', message_count:900, user_message_count:75, updated_at:20, last_message_at:20, _lineage_root_id:'seg1', _compression_segment_count:2, pre_compression_snapshot:true},
  {session_id:'seg3', title:'Graphify', parent_session_id:'seg2', message_count:1400, user_message_count:118, _lineage_user_message_count:118, updated_at:30, last_message_at:30, _lineage_root_id:'seg1', _compression_segment_count:3},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
const segmentSum = collapsed[0]._lineage_segments
  .reduce((total, seg) => total + seg.user_message_count, 0);
console.log(JSON.stringify({
  sid: collapsed[0].session_id,
  segments: collapsed[0]._lineage_segments.map(seg => seg.session_id),
  segmentSum,
  rendered: metaTextFor(collapsed[0], 'detailed'),
}));
""")))
    assert result["sid"] == "seg3"
    assert result["segments"] == ["seg3", "seg2", "seg1"]
    assert result["segmentSum"] == 233
    assert result["rendered"] == "1400 msgs · 118 from you"


@requires_node
def test_compressed_fork_continuation_lineage_renders_the_tip_count():
    """A forked conversation's own compression segments share a lineage key.

    Mirrors the fixture at ``tests/test_session_lineage_collapse.py:243-246``:
    the forked conversation compresses, so its segments carry a shared
    ``_lineage_root_id``. (``session_source:'fork'`` rows are excluded from the
    collapse by ``_sessionLineageKey``, so they never reach this branch.)
    """
    result = json.loads(_run_node(_harness("""
const sessions = [
  {session_id:'fork-seg13', title:'Release review (fork)', message_count:2490, user_message_count:180, updated_at:200, last_message_at:200, pre_compression_snapshot:true, _lineage_root_id:'fork-root', _compression_segment_count:13},
  {session_id:'fork-seg14', title:'Release review (fork)', parent_session_id:'fork-seg13', message_count:2532, user_message_count:191, _lineage_user_message_count:191, updated_at:150, last_message_at:150, _lineage_root_id:'fork-root', _compression_segment_count:14},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
const segmentSum = collapsed[0]._lineage_segments
  .reduce((total, seg) => total + seg.user_message_count, 0);
console.log(JSON.stringify({
  rows: collapsed.map(row => row.session_id),
  lineageKey: collapsed[0]._lineage_key,
  segmentSum,
  lineageUserTurns: collapsed[0]._lineage_user_message_count,
  rendered: metaTextFor(collapsed[0], 'detailed'),
}));
""")))
    assert result["rows"] == ["fork-seg14"]
    assert result["lineageKey"] == "fork-root"
    assert result["segmentSum"] == 371
    assert result["lineageUserTurns"] == 191
    assert result["rendered"] == "2532 msgs · 191 from you"


@requires_node
def test_imported_messaging_row_without_user_message_count_renders_no_label():
    """Imported/restored rows arrive with `user_message_count: null` from the API."""
    result = json.loads(_run_node(_harness("""
const row = {session_id:'tg', title:'Telegram thread', message_count:12, user_message_count:null, session_source:'messaging', raw_source:'telegram', source_label:'Telegram', updated_at:10, last_message_at:10, read_only:true};
console.log(JSON.stringify({rendered: metaTextFor(row, 'detailed')}));
""")))
    assert result["rendered"] == "12 msgs · Telegram · read-only"


@requires_node
def test_local_turn_owner_is_idempotent_across_initial_title_and_stream_upserts():
    result = json.loads(_run_node(_harness("""
const _localTurnOwners = new Map();
var _allSessions = [{session_id:'send', message_count:8, user_message_count:2, _lineage_segments:[{}], _lineage_user_message_count:2}];
S = {session:{session_id:'send', message_count:8, user_message_count:2, _lineage_segments:[{}], _lineage_user_message_count:2}, messages:[{},{}]};
eval(extractFunc('resolveLocalTurnCountOwner'));
const upsertStart=src.indexOf('function upsertActiveSessionForLocalTurn');
const upsertEnd=src.indexOf('\\nfunction _sessionRowsWithActiveEphemeralSession', upsertStart);
eval(src.slice(upsertStart, upsertEnd));
const owner = resolveLocalTurnCountOwner();
upsertActiveSessionForLocalTurn({messageCount:9, userTurnOwner:owner});
upsertActiveSessionForLocalTurn({messageCount:9, userTurnOwner:owner});
upsertActiveSessionForLocalTurn({messageCount:9, userTurnOwner:owner});
console.log(JSON.stringify({direct:S.session.user_message_count, logical:_allSessions[0]._lineage_user_message_count, token:owner.token}));
""")))
    assert result["direct"] == 3
    assert result["logical"] == 3
    assert result["token"]


@requires_node
def test_local_turn_owner_unknown_baseline_rolls_back_without_inventing_zero():
    result = json.loads(_run_node(_harness("""
const _localTurnOwners = new Map();
var _allSessions = [{session_id:'unknown', message_count:3, _lineage_segments:[{}]}];
S = {session:{session_id:'unknown', message_count:3}, messages:[{}]};
eval(extractFunc('resolveLocalTurnCountOwner'));
eval(extractFunc('clearLocalTurnCountOwner'));
eval(extractFunc('restoreLocalTurnCountOwner'));
const owner = resolveLocalTurnCountOwner();
restoreLocalTurnCountOwner('unknown');
console.log(JSON.stringify({
  direct: S.session.user_message_count ?? null,
  rowHasDirect: Object.prototype.hasOwnProperty.call(_allSessions[0], 'user_message_count'),
  baseline: owner.baseline.directCount,
}));
""")))
    assert result["direct"] is None
    assert result["rowHasDirect"] is False
    assert result["baseline"] is None


@requires_node
def test_local_turn_owner_is_cleared_when_idle_refresh_confirms_the_turn():
    result = json.loads(_run_node(_harness("""
const _localTurnOwners = new Map();
var _allSessions = [{session_id:'idle', message_count:8, user_message_count:2, is_streaming:true}];
var _sendInProgress = false;
var _sendInProgressSid = null;
var INFLIGHT = {};
S = {session:{session_id:'idle', message_count:8, user_message_count:2}, messages:[{}], busy:false};
function _isOptimisticFirstTurnSessionRow(){ return true; }
function _shouldKeepLocalOnlyOptimisticSessionRow(){ return false; }
function _isServerIdleSessionRow(s){ return !!s && !s.is_streaming && !s.active_stream_id && !s.pending_user_message && !s.has_pending_user_message && !s.pending_started_at; }
function _dropStaleOptimisticSessionRow(){}
eval(extractFunc('resolveLocalTurnCountOwner'));
eval(extractFunc('clearLocalTurnCountOwner'));
eval(extractFunc('_mergeOptimisticFirstTurnSessions'));
const first = resolveLocalTurnCountOwner();
const merged = _mergeOptimisticFirstTurnSessions([{session_id:'idle', message_count:9, user_message_count:3}]);
_allSessions = merged;
S.session.user_message_count = 3;
const second = resolveLocalTurnCountOwner();
console.log(JSON.stringify({first:first.directCount, second:second.directCount, different:first.token!==second.token}));
""")))
    assert result == {"first": 3, "second": 4, "different": True}


def test_incomplete_sidecar_lineage_preserves_child_display_and_skips_count(monkeypatch):
    child = SimpleNamespace(
        session_id="missing-parent-child",
        parent_session_id="missing-parent",
        session_source="webui",
        messages=[{"role": "user", "content": "child only"}],
    )
    monkeypatch.setattr(routes.Session, "load", lambda _sid: None)
    assert routes._webui_sidecar_lineage_messages_for_display(child) == child.messages
    assert routes._merged_session_messages_for_display(child) == child.messages
    assert routes._merged_webui_lineage_messages_for_display(child, child.messages) == child.messages
    row = {"session_id": child.session_id, "_lineage_tip_id": child.session_id}
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: [])
    routes._project_sidebar_lineage_user_counts([row])
    assert "_lineage_user_message_count" not in row


def test_sidebar_lineage_count_matches_detail_merge_and_fails_closed_for_unknown_state_rows(monkeypatch):
    tip = SimpleNamespace(
        session_id="lineage-tip",
        parent_session_id="lineage-parent",
        profile="default",
        session_source="webui",
        messages=[{"role": "user", "content": "sidecar turn", "timestamp": 1.0}],
        truncation_watermark=None,
        truncation_boundary=None,
    )
    parent = SimpleNamespace(
        session_id="lineage-parent",
        parent_session_id=None,
        pre_compression_snapshot=True,
        session_source="webui",
        messages=[],
    )
    monkeypatch.setattr(routes.Session, "load", lambda sid: {"lineage-tip": tip, "lineage-parent": parent}.get(sid))
    row = {"session_id": "lineage-tip", "_lineage_tip_id": "lineage-tip", "parent_session_id": "lineage-parent"}
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: [
        {"role": "user", "timestamp": 2.0},
    ])
    routes._project_sidebar_lineage_user_counts([row])
    assert "_lineage_user_message_count" not in row

    row = {"session_id": "lineage-tip", "_lineage_tip_id": "lineage-tip", "parent_session_id": "lineage-parent"}
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: [
        {
            "role": "user",
            "content": "state turn",
            "timestamp": 2.0,
            "display_kind": None,
            "source": None,
            "_source": None,
            "_compressed_summary": 0,
        },
    ])
    routes._project_sidebar_lineage_user_counts([row])
    assert row["_lineage_user_message_count"] == 2

    row = {"session_id": "lineage-tip", "_lineage_tip_id": "lineage-tip", "parent_session_id": "lineage-parent"}
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: [
        {"role": "user", "content": '[{"type":"text","text":"structured"}]'},
    ])
    routes._project_sidebar_lineage_user_counts([row])
    assert "_lineage_user_message_count" not in row


def test_partial_incomplete_sidecar_lineage_preserves_collected_ancestors(monkeypatch):
    child = SimpleNamespace(
        session_id="a" * 32,
        parent_session_id="b" * 32,
        session_source="webui",
        messages=[{"role": "user", "content": "new turn", "timestamp": 2.0}],
    )
    parent = SimpleNamespace(
        session_id="b" * 32,
        parent_session_id="c" * 32,
        pre_compression_snapshot=True,
        session_source="webui",
        messages=[{"role": "user", "content": "old turn", "timestamp": 1.0}],
    )
    monkeypatch.setattr(
        routes.Session,
        "load",
        lambda sid: {"b" * 32: parent}.get(sid),
    )
    assert [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)] == [
        "old turn",
        "new turn",
    ]


def test_process_wakeup_restamp_preserves_count_and_advances_data_version(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER, started_at REAL, source TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, display_kind TEXT, source TEXT, _source TEXT, _compressed_summary INTEGER, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES ('w', 'Wakeup', 'm', 1, 1, 'cli')")
    conn.execute("INSERT INTO messages VALUES (1, 'w', 'user', 'wake', '', 'process_wakeup', NULL, 0, 1)")
    conn.commit()
    before = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    version_before = models._sqlite_data_version(db)
    conn.execute("UPDATE messages SET display_kind = 'gateway' WHERE id = 1")
    conn.commit()
    after = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    version_after = models._sqlite_data_version(db)
    conn.close()
    assert before["actual_user_message_count"] == after["actual_user_message_count"] == 0
    assert version_after != version_before


# ---------------------------------------------------------------------------
# Producer accuracy: the sidebar label is a factual claim, so a row that cannot
# separate user turns from total messages must report "unknown", not the total.
# ---------------------------------------------------------------------------


def _make_state_db(path, *, with_messages_table=True, with_role=True,
                   with_session_id=True, sessions=(), messages=()):
    """Minimal state.db in one of the four supported schema shapes (#3762)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, session_source TEXT)"
    )
    for row in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, "
            "source, session_source) VALUES (?,?,?,?,?,?,?)", row
        )
    if with_messages_table:
        cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
        if with_session_id:
            cols.append("session_id TEXT")
        if with_role:
            cols.append("role TEXT")
        cols.append("timestamp REAL")
        cols.append("content TEXT")
        cols.append("display_kind TEXT")
        cols.append("source TEXT")
        cols.append("_source TEXT")
        cols.append("_compressed_summary INTEGER")
        conn.execute(f"CREATE TABLE messages ({', '.join(cols)})")
        for sid, role, ts in messages:
            names, values = [], []
            if with_session_id:
                names.append("session_id"); values.append(sid)
            if with_role:
                names.append("role"); values.append(role)
            names.append("timestamp"); values.append(ts)
            names.append("content"); values.append(f"message {ts}")
            names.append("display_kind"); values.append(None)
            names.append("source"); values.append(None)
            names.append("_source"); values.append(None)
            names.append("_compressed_summary"); values.append(0)
            conn.execute(
                f"INSERT INTO messages ({', '.join(names)}) "
                f"VALUES ({', '.join('?' * len(values))})", values
            )
    conn.commit()
    conn.close()


_SEVEN = [("s1", "user", 1.0), ("s1", "assistant", 2.0), ("s1", "tool", 3.0),
          ("s1", "assistant", 4.0), ("s1", "user", 5.0), ("s1", "assistant", 6.0),
          ("s1", "tool", 7.0)]
# A default CLI title, so `is_cli_session_row` cannot short-circuit at the
# `_looks_like_default_cli_title` branch and the `_count_user_turns` gate is
# the assertion that actually decides visibility.
_SESSION = [("s1", "CLI Session", "gpt", 7, 1000.0, "cli", "cli")]


def test_role_column_present_reports_real_user_turn_count(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=_SESSION, messages=_SEVEN)
    row = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    assert row["actual_message_count"] == 7
    assert row["actual_user_message_count"] == 2


def test_state_db_missing_content_column_reports_unknown_user_turns(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, session_source TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', 'CLI Session', 'gpt', 1, 1, 'cli', 'cli')"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "display_kind TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 's1', 'user', NULL, 1)"
    )
    conn.commit()
    conn.close()

    row = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    assert row["actual_user_message_count"] is None


def test_real_agent_state_db_schema_reports_text_user_turns(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
        "message_count INTEGER, started_at REAL, source TEXT, session_source TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', 'CLI Session', 'gpt', 2, 1, 'cli', 'cli')"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL, active INTEGER, compacted INTEGER)"
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?, 's1', ?, ?, ?, 1, 0)",
        [
            (1, "user", "ordinary", 1),
            (2, "assistant", '[{"type":"text","text":"reply"}]', 2),
            (3, "user", "again", 3),
        ],
    )
    conn.commit()
    conn.close()

    row = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    assert row["actual_user_message_count"] == 2


def test_compact_uses_the_canonical_human_turn_truth_table():
    session = models.Session(session_id="truth-table")
    session.messages = [
        {"role": "user", "content": "ordinary"},
        {"role": "user", "content": "hidden", "display_kind": "gateway"},
        {"role": "user", "content": "wake", "_source": "process_wakeup"},
        {"role": "user", "content": "summary", "_compressed_summary": True},
        {"role": "user", "content": "[context compaction: old]"},
        {"role": "user", "content": "[Your active task list was preserved across context compression]\n- [ ] task"},
        {"role": "user", "content": "Continue from the compressed conversation context above. This marker exists because no human user turn was available."},
        {"role": "user", "content": "Continue from the compressed conversation context above. This marker exists because the compacted transcript contained no preserved user turn."},
        {"role": "user", "content": {"type": "text", "text": "structured"}},
    ]
    assert session.compact()["user_message_count"] == 2


def test_context_compaction_prose_remains_a_human_turn():
    assert agent_sessions.is_human_user_turn({
        "role": "user",
        "content": "Context compaction is broken, please fix it",
    })


@pytest.mark.parametrize("content", [None, [], [{"type": "image_url"}], {"type": "tool_use", "id": "x"}, {"type": "text", "value": "missing text key"}])
def test_human_turn_classifier_fails_closed_for_empty_or_unsupported_content(content):
    assert not agent_sessions.is_human_user_turn({"role": "user", "content": content})


@pytest.mark.parametrize("shape", ["no_role_column", "no_messages_table", "no_session_id"])
def test_degraded_schemas_report_unknown_user_turns_not_the_total(tmp_path, shape):
    """Without a ``role`` column the two are indistinguishable.

    Reporting the total here would render ``7 msgs · 7 from you``, asserting
    that every message was a user turn. NULL makes the sidebar drop the label.
    """
    db = tmp_path / "state.db"
    kwargs = {"no_role_column": dict(with_role=False),
              "no_messages_table": dict(with_messages_table=False),
              "no_session_id": dict(with_session_id=False)}[shape]
    _make_state_db(db, sessions=_SESSION,
                   messages=() if shape == "no_messages_table" else _SEVEN, **kwargs)
    rows = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)
    assert {r["id"] for r in rows} == {"s1"}, "the row must still surface (#3762)"
    assert rows[0]["actual_user_message_count"] is None
    assert rows[0]["actual_message_count"] == 7


@pytest.mark.parametrize("shape", ["no_role_column", "no_messages_table", "no_session_id"])
def test_degraded_schemas_keep_their_cli_visibility_heuristic(tmp_path, shape):
    """`_count_user_turns` gated row visibility on the same alias.

    It must keep seeing a coarse non-zero value, or these rows vanish.
    """
    db = tmp_path / "state.db"
    kwargs = {"no_role_column": dict(with_role=False),
              "no_messages_table": dict(with_messages_table=False),
              "no_session_id": dict(with_session_id=False)}[shape]
    _make_state_db(db, sessions=_SESSION,
                   messages=() if shape == "no_messages_table" else _SEVEN, **kwargs)
    row = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]
    assert agent_sessions._count_user_turns(row) == 7
    assert agent_sessions.is_cli_session_row_visible(row) is True


def test_count_user_turns_fallback_does_not_reach_sidecar_or_index_rows():
    """The coarse fallback is gated on the state.db projection's own column.

    An ``_index.json`` or sidecar row carries neither ``actual_user_message_count``
    nor ``actual_message_count``, so it must still fall through to the message
    scan rather than picking up a total from an unrelated key.
    """
    index_row = {"id": "s1", "title": "CLI Session", "message_count": 7,
                 "messages": [{"role": "user"}, {"role": "assistant"}]}
    assert "actual_user_message_count" not in index_row
    assert agent_sessions._count_user_turns(index_row) == 1

    # A row that reached the projection and got NULL still uses the coarse total.
    projected = {"id": "s1", "title": "CLI Session",
                 "actual_user_message_count": None, "actual_message_count": 7}
    assert agent_sessions._count_user_turns(projected) == 7


@pytest.mark.parametrize("shape", ["no_role_column", "no_messages_table", "no_session_id"])
@pytest.mark.parametrize("source", ["cli", "cron", "webhook"])
def test_projected_sidebar_row_stays_visible_on_degraded_schemas(tmp_path, shape, source):
    """The visibility gate also runs on the PROJECTED row, not just the raw one.

    `api/routes.py` filters with `is_cli_session_row_visible` on the dicts built
    in `_load_cli_sessions_uncached`, which rename `actual_user_message_count`
    to `user_message_count`. If a projection drops the original key, the coarse
    fallback cannot fire there and a degraded-schema row disappears from the
    sidebar, which is the #3762 fault class one layer downstream.

    Parametrized over `source` because that function builds the row through
    three separate projections (the main CLI pass, the cron branch, and the
    second-pass webhook fetch); a fix applied to only one of them would leave
    the other two carrying the bug.
    """
    db = tmp_path / "state.db"
    kwargs = {"no_role_column": dict(with_role=False),
              "no_messages_table": dict(with_messages_table=False),
              "no_session_id": dict(with_session_id=False)}[shape]
    sessions = [("s1", "CLI Session", "gpt", 7, 1000.0, source, source)]
    _make_state_db(db, sessions=sessions,
                   messages=() if shape == "no_messages_table" else _SEVEN, **kwargs)
    raw = agent_sessions.read_importable_agent_session_rows(db, exclude_sources=None)[0]

    # Drive the REAL projection rather than hand-building its output, so this
    # fails if `_load_cli_sessions_uncached` stops carrying the discriminator.
    projected_rows = models._load_cli_sessions_uncached(
        tmp_path, db, None, None, include_claude_code=False
    )
    projected = next(r for r in projected_rows if r.get("session_id") == raw["id"])

    assert projected["user_message_count"] is None, "the renamed copy reports unknown"
    assert agent_sessions._count_user_turns(projected) == 7
    assert agent_sessions.is_cli_session_row_visible(projected) is True
