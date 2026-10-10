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
// Real source classifiers: the partition and the attach step must agree on the sidebar bucket.
eval(src.match(/const _MESSAGING_RAW_SOURCES = [^;]*;/)[0].replace('const ', 'global.'));
eval(extractFunc('_isMessagingSession'));
eval(extractFunc('_isWebUiSourceSession'));
eval(extractFunc('_isExternalSession'));
eval(extractFunc('_isCliSession'));
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
eval(extractFunc('_isDelegatedSubagentRow'));
eval(extractFunc('_sidebarProjectResolver'));
eval(extractFunc('_sidebarRowsById'));
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
    # The child inherits its parent's project, so the "no project" filter drops both...
    assert out["sessionsRaw"] == []
    # ...and it is NOT rendered as a top-level orphan.
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


def test_5305_flagless_subagent_child_of_filtered_parent_is_suppressed():
    """A delegated subagent row can arrive without ``_cross_surface_child_session``
    (all-profiles payloads, same-source subagent->subagent edges). When
    ``parent_source`` proves the importer saw the parent, the child follows its
    out-of-view parent instead of leaking as a top-level "Subagent Session"."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global.S = { session: null, busy: false, activeStreamId: null };
global._activeProject = global.NO_PROJECT_FILTER;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const allMatched = [
  { session_id:'proj_parent', title:'Parent WebUI', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:5, project_id:'projX', profile:'other', updated_at:100, last_message_at:100 },
  { session_id:'orchestrator', title:'Subagent Session', parent_session_id:'proj_parent', relationship_type:'child_session', parent_source:'webui', raw_source:'subagent', source_tag:'subagent', session_source:'other', profile:'other', message_count:3, updated_at:101, last_message_at:101 },
  { session_id:'leaf', title:'Subagent Session', parent_session_id:'orchestrator', relationship_type:'child_session', parent_source:'subagent', raw_source:'subagent', source_tag:'subagent', session_source:'other', profile:'other', message_count:2, updated_at:102, last_message_at:102 },
];
const part = _partitionSidebarSessionRows(allMatched, null);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
const direct = _attachChildSessionsToSidebarRows([], allMatched.slice(1));
console.log(JSON.stringify({
  sessionsRaw: part.sessionsRaw.map(s=>s.session_id),
  topLevel: rows.map(r=>r.session_id),
  directTopLevel: direct.map(r=>r.session_id),
}));
"""
    out = json.loads(_run_node(source))
    # The partition already drops project-inheriting children (#7765); attach must suppress them too.
    assert out["sessionsRaw"] == []
    assert out["topLevel"] == []
    assert out["directTopLevel"] == []


def test_5305_flagless_subagent_child_still_stacks_under_visible_parent():
    """Suppression only applies when the parent is out of view: the same
    flag-less subagent child nests under its parent once that parent is rendered."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._showArchived = false;
const collapsed = [{ session_id:'webui_parent', title:'Parent WebUI', raw_source:'webui', source_tag:'webui', session_source:'webui', message_count:3 }];
const raw = [
  collapsed[0],
  { session_id:'subagent_child', title:'Subagent Session', parent_session_id:'webui_parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:2 },
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


def test_5305_flagless_subagent_child_of_unimported_parent_still_orphans():
    """Without ``parent_source`` the importer never saw the parent (outside the
    recency window), so the child stays an openable orphan row rather than vanishing."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._showArchived = false;
const raw = [
  { session_id:'leaf', title:'Leaf', parent_session_id:'orch', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:2 },
];
const rows = _attachChildSessionsToSidebarRows([], raw);
console.log(JSON.stringify(rows.map(r=>({sid:r.session_id, orphan:!!r._orphan_child_session}))));
"""
    out = json.loads(_run_node(source))
    assert out == [{"sid": "leaf", "orphan": True}]


def _importer_rows_to_sidebar(rows):
    return [
        {
            "session_id": r["id"],
            "title": r.get("title"),
            "parent_session_id": r.get("parent_session_id"),
            "relationship_type": r.get("relationship_type"),
            "parent_source": r.get("parent_source"),
            "raw_source": r.get("source"),
            "source_tag": r.get("source"),
            "session_source": "other",
            "message_count": r.get("actual_message_count") or 2,
        }
        for r in rows
    ]


@pytest.mark.parametrize("filler,expect_orphan", [(22, False), (23, True)])
def test_5305_importer_window_decides_flagless_subagent_orphaning(tmp_path, filler, expect_orphan):
    """Drives the real importer: a parent inside the oversample is known and the
    child nests; a parent beyond it is unknown and the child stays a top-level row."""
    from api.agent_sessions import read_importable_agent_session_rows
    from tests.test_subagent_parent_in_import_window import _window_db

    db = tmp_path / "state.db"
    _window_db(db, filler=filler)
    imported = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    raw = [r for r in _importer_rows_to_sidebar(imported) if r["session_id"] in ("orch", "leaf")]
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._showArchived = false;
const raw = {json.dumps(raw)};
const collapsed = raw.filter(r=>!r.parent_session_id);
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}}))));
"""
    out = json.loads(_run_node(source))
    if expect_orphan:
        assert out == [{"sid": "leaf", "orphan": True, "kids": []}]
    else:
        assert out == [{"sid": "orch", "orphan": False, "kids": ["leaf"]}]


@pytest.mark.parametrize("filler,expect_orphan", [(22, False), (23, True)])
def test_5305_enrichment_keeps_importer_parent_source(tmp_path, monkeypatch, filler, expect_orphan):
    """Importer -> lineage enrichment -> renderer: enrichment must not fill in the
    ``parent_source`` of a parent the importer left out, or the child disappears."""
    import sqlite3
    import api.models as models
    from tests.test_subagent_parent_in_import_window import _window_db

    db = tmp_path / "state.db"
    _window_db(db, filler=filler)
    with sqlite3.connect(str(db)) as conn:  # lineage enrichment needs these columns
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        conn.execute("ALTER TABLE sessions ADD COLUMN end_reason TEXT")
    rows = models._load_cli_sessions_uncached(
        tmp_path, db, None, visible_session_limit=3, include_claude_code=False
    )
    rows = [r for r in rows if r["session_id"] in ("orch", "leaf")]
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    models._enrich_sidebar_lineage_metadata(rows)
    leaf = next(r for r in rows if r["session_id"] == "leaf")
    assert leaf["parent_source"] == (None if expect_orphan else "subagent")
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._showArchived = false;
const raw = {json.dumps(rows, default=str)};
const rows = _attachChildSessionsToSidebarRows(raw.filter(r=>!r.parent_session_id), raw);
console.log(JSON.stringify(rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}}))));
"""
    out = json.loads(_run_node(source))
    if expect_orphan:
        assert out == [{"sid": "leaf", "orphan": True, "kids": []}]
    else:
        assert out == [{"sid": "orch", "orphan": False, "kids": ["leaf"]}]


def test_5305_search_keeps_matching_subagent_when_parent_does_not_match():
    """While sidebar search is active, a delegated subagent that matches the query
    stays openable even though its known parent does not match and is not rendered."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
eval(extractFunc('_stripAttachedFilesMarker'));
eval(extractFunc('_sessionDisplayTitle'));
eval(extractFunc('_sessionSearchAddIdCandidate'));
eval(extractFunc('_sessionSearchCleanUrlToken'));
eval(extractFunc('_sessionSearchSessionIdCandidates'));
eval(extractFunc('_sessionSearchDirectSessionMatches'));
eval(extractFunc('_sessionSearchDirectAndTitleMatches'));
eval(extractFunc('_sessionSearchMergeMatches'));
global.S = { session: null, busy: false, activeStreamId: null };
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const all = [
  { session_id:'parent', title:'Plan the release', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:5, updated_at:100, last_message_at:100 },
  { session_id:'sub', title:'Zebra benchmark notes', parent_session_id:'parent', relationship_type:'child_session', parent_source:'webui', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:3, updated_at:101, last_message_at:101 },
  { session_id:'subx', title:'Zebra flagged notes', parent_session_id:'parent', relationship_type:'child_session', parent_source:'webui', raw_source:'subagent', source_tag:'subagent', session_source:'other', _cross_surface_child_session:true, message_count:3, updated_at:102, last_message_at:102 },
];
function render(query){
  global.$ = (id)=>id==='sessionSearch' ? { value: query } : null;
  const matched = _sessionSearchMergeMatches(all, query, []);
  const part = _partitionSidebarSessionRows(matched, null);
  return _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw)
    .map(r=>({sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}));
}
console.log(JSON.stringify({ search: render('zebra'), idle: render('') }));
"""
    out = json.loads(_run_node(source))
    assert sorted(out["search"], key=lambda r: r["sid"]) == [
        {"sid": "sub", "orphan": True, "kids": []},
        {"sid": "subx", "orphan": True, "kids": []},
    ]
    assert len(out["idle"]) == 1 and out["idle"][0]["sid"] == "parent"
    assert sorted(out["idle"][0]["kids"]) == ["sub", "subx"]


@pytest.mark.parametrize("parent_source", ["cli", "tui", "acp"])
def test_5305_subagent_of_cli_parent_stays_reachable_in_all_profiles(parent_source):
    """All-profiles payloads carry no cross-surface flag. The partition puts a
    CLI/TUI parent in the CLI bucket and its subagent in the WebUI bucket, so the
    child can never attach there and must stay an openable orphan row."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._activeProject = null;
global._showArchived = false;
global.window = {{ _showCliSessions: true }};
const allMatched = [
  {{ session_id:'cli_parent', title:'CLI run', session_source:'cli', raw_source:'{parent_source}', source_tag:'{parent_source}', is_cli_session:true, profile:'a', message_count:5, updated_at:100, last_message_at:100 }},
  {{ session_id:'sub', title:'Subagent Session', parent_session_id:'cli_parent', relationship_type:'child_session', parent_source:'{parent_source}', raw_source:'subagent', source_tag:'subagent', session_source:'other', profile:'a', message_count:3, updated_at:101, last_message_at:101 }},
];
const out = {{}};
for (const tab of ['webui', 'cli']) {{
  global._sessionSourceFilter = tab;
  const part = _partitionSidebarSessionRows(allMatched, null);
  const ref = tab === 'cli' ? part.cliReferenceRaw : part.webuiReferenceRaw;
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, ref, part.rowsById);
  out[tab] = rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}}));
}}
console.log(JSON.stringify(out));
"""
    out = json.loads(_run_node(source))
    assert out["webui"] == [{"sid": "sub", "orphan": True, "kids": []}]
    assert out["cli"] == [{"sid": "cli_parent", "orphan": False, "kids": []}]


@pytest.mark.parametrize("parent", [
    # A claimed Desktop sidecar keeps is_cli_session:true with a non-literal raw source.
    {"raw_source": "desktop", "session_source": "other", "is_cli_session": True},
    # A Claude Code import is CLI-bucketed through session_source, not the raw source.
    {"raw_source": "claude_code", "session_source": "external_agent", "is_cli_session": True},
])
def test_5305_subagent_of_cli_bucket_parent_with_non_literal_source_stays_reachable(parent):
    """The partition files the parent in the CLI bucket via its normalized row metadata
    (is_cli_session / session_source), so the attach step must classify the SAME row, not a
    synthetic ``{raw_source: parent_source}``; otherwise the child is suppressed in the WebUI
    tab and absent from the CLI tab, i.e. unreachable."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._activeProject = null;
global._showArchived = false;
global.window = {{ _showCliSessions: true }};
const parentMeta = {json.dumps(parent)};
const allMatched = [
  {{ session_id:'p', title:'Parent', source_tag:parentMeta.raw_source, profile:'a', message_count:5, updated_at:100, last_message_at:100, ...parentMeta }},
  {{ session_id:'sub', title:'Subagent Session', parent_session_id:'p', relationship_type:'child_session', parent_source:parentMeta.raw_source, raw_source:'subagent', source_tag:'subagent', session_source:'other', profile:'a', message_count:3, updated_at:101, last_message_at:101 }},
];
const out = {{}};
for (const tab of ['webui', 'cli']) {{
  global._sessionSourceFilter = tab;
  const part = _partitionSidebarSessionRows(allMatched, null);
  const ref = tab === 'cli' ? part.cliReferenceRaw : part.webuiReferenceRaw;
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, ref, part.rowsById);
  out[tab] = rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}}));
}}
console.log(JSON.stringify(out));
"""
    out = json.loads(_run_node(source))
    assert out["cli"] == [{"sid": "p", "orphan": False, "kids": []}]
    assert out["webui"] == [{"sid": "sub", "orphan": True, "kids": []}]


def test_5305_flagless_subagent_of_zero_message_webui_parent_in_payload_is_suppressed():
    """The parent's real payload row decides: a WebUI parent that is in the payload but not
    rendered (no visible messages) shares the child's bucket, so the child follows it."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + """
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const allMatched = [
  { session_id:'p', title:'Parent', session_source:'webui', raw_source:'webui', source_tag:'webui', message_count:0, updated_at:100 },
  { session_id:'sub', title:'Subagent Session', parent_session_id:'p', relationship_type:'child_session', parent_source:'webui', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:3, updated_at:101, last_message_at:101 },
];
const part = _partitionSidebarSessionRows(allMatched, null);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw, part.rowsById);
console.log(JSON.stringify({ sessionsRaw: part.sessionsRaw.map(s=>s.session_id), topLevel: rows.map(r=>r.session_id) }));
"""
    out = json.loads(_run_node(source))
    assert out["sessionsRaw"] == ["sub"]
    assert out["topLevel"] == []


@pytest.mark.parametrize("parent_source", ["cron", "webhook", "kanban", "tool", "api_server", "telegram"])
def test_5305_flagless_subagent_of_filtered_non_cli_parent_is_suppressed(parent_source):
    """Any non-CLI parent shares the WebUI bucket with its subagent (``_isCliSession``
    decides the partition), so when that parent is filtered out the flag-less child
    must follow it instead of leaking as a top-level "Subagent Session"."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global.S = {{ session: null, busy: false, activeStreamId: null }};
global._activeProject = null;
global._showArchived = false;
global._sessionSourceFilter = 'webui';
const allMatched = [
  {{ session_id:'p', title:'Parent', session_source:'other', raw_source:'{parent_source}', source_tag:'{parent_source}', default_hidden:true, message_count:5, updated_at:100, last_message_at:100 }},
  {{ session_id:'sub', title:'Subagent Session', parent_session_id:'p', relationship_type:'child_session', parent_source:'{parent_source}', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:3, updated_at:101, last_message_at:101 }},
];
const part = _partitionSidebarSessionRows(allMatched, null);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw, part.rowsById);
console.log(JSON.stringify({{ sessionsRaw: part.sessionsRaw.map(s=>s.session_id), topLevel: rows.map(r=>r.session_id) }}));
"""
    out = json.loads(_run_node(source))
    assert out["sessionsRaw"] == ["sub"]  # the child reaches attach; the parent is filtered out
    assert out["topLevel"] == []


def _compressed_parent_db(path, newer):
    """Compressed subagent parent (orch -> orch_tip), a child linked to the OLD segment, `newer` newer rows."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER, "
        "started_at REAL, source TEXT, parent_session_id TEXT, ended_at REAL, end_reason TEXT)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    rows = [
        ("orch", "Orchestrator", 100.0, "subagent", None, 150.0, "compression"),
        ("orch_tip", "Orchestrator", 151.0, "subagent", "orch", None, None),
        # Delegated before the compression: it names the parent's old segment.
        ("leaf", "Leaf", 120.0, "subagent", "orch", None, None),
    ]
    rows += [(f"new{i}", f"Newer {i}", 300.0 + i, "subagent", None, None, None) for i in range(newer)]
    for sid, title, started, source, parent, ended, reason in rows:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, parent_session_id, "
            "ended_at, end_reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, started, source, parent, ended, reason),
        )
    ts = {"orch": 110.0, "orch_tip": 160.0, "leaf": 9000.0}
    ts.update({f"new{i}": 400.0 + i for i in range(newer)})
    for sid, t in ts.items():
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "user", t))
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "assistant", t + 0.5))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("newer,nested", [(19, True), (160, False)])
def test_5305_subagent_of_compressed_parent_stays_reachable_at_default_limit(tmp_path, monkeypatch, newer, nested):
    """Re-gate: the child links to the parent's pre-compression segment while the sidebar
    projects the parent under its compression tip. With the default 20-row window and 19
    newer rows the child must still be reachable (nested under the parent, or an orphan)."""
    import api.models as models

    db = tmp_path / "state.db"
    _compressed_parent_db(db, newer=newer)
    rows = models._load_cli_sessions_uncached(
        tmp_path, db, None, visible_session_limit=20, include_claude_code=False
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    models._enrich_sidebar_lineage_metadata(rows)
    ids = [r["session_id"] for r in rows]
    assert "leaf" in ids
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._showArchived = false;
const raw = {json.dumps(rows, default=str)};
const rows = _attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(raw), raw);
const top = rows.map(r=>r.session_id);
const nested = rows.flatMap(r=>(r._child_sessions||[]).map(c=>[r.session_id, c.session_id]));
console.log(JSON.stringify({{top, nested}}));
"""
    out = json.loads(_run_node(source))
    reachable = "leaf" in out["top"] or any(c == "leaf" for _, c in out["nested"])
    assert reachable, out
    if nested:  # parent inside the oversample: re-added under its tip id, child nests
        assert out["nested"] == [["orch_tip", "leaf"]], out
    else:  # parent beyond the limit * 8 oversample: child stays an openable top-level row
        assert "leaf" in out["top"] and out["nested"] == [], out


def _desktop_parent_db(path, newer, parent_source="desktop"):
    """A Desktop (or other-source) parent that went quiet, its delegated child, and `newer` hotter unrelated rows."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER, "
        "started_at REAL, source TEXT, parent_session_id TEXT, ended_at REAL, end_reason TEXT)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    rows = [("desk", "Desktop run", 100.0, parent_source, None), ("leaf", "Leaf", 120.0, "subagent", "desk")]
    rows += [(f"new{i}", f"Newer {i}", 300.0 + i, "subagent", None) for i in range(newer)]
    for sid, title, started, source, parent in rows:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, parent_session_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, started, source, parent),
        )
    ts = {"desk": 110.0, "leaf": 9000.0}
    ts.update({f"new{i}": 400.0 + i for i in range(newer)})
    for sid, t in ts.items():
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "user", t))
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "assistant", t + 0.5))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("newer", [19, 25])
def test_5305_subagent_of_desktop_parent_outside_window_stays_reachable(tmp_path, newer):
    """Re-gate: the Desktop parent is in the oversample but not the 20-row slice, and the
    parent-recovery loop only re-adds subagent parents. The child must not keep claiming
    ``parent_source='desktop'`` (\"parent is in this payload\"), or the all-profiles sidebar
    drops it from both tabs; it must stay an openable row."""
    import api.models as models

    db = tmp_path / "state.db"
    _desktop_parent_db(db, newer=newer)
    rows = models._load_cli_sessions_uncached(
        tmp_path, db, None, visible_session_limit=20, include_claude_code=False
    )
    # The all-profiles loader merges these rows as-is (no lineage enrichment, so no cross-surface flag).
    ids = [r["session_id"] for r in rows]
    assert "leaf" in ids and "desk" not in ids, ids
    leaf = next(r for r in rows if r["session_id"] == "leaf")
    assert leaf["parent_source"] is None
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._activeProject = null;
global._showArchived = false;
global.window = {{ _showCliSessions: true }};
const allMatched = {json.dumps(rows, default=str)};
const out = {{}};
for (const tab of ['webui', 'cli']) {{
  global._sessionSourceFilter = tab;
  const part = _partitionSidebarSessionRows(allMatched, null);
  const ref = tab === 'cli' ? part.cliReferenceRaw : part.webuiReferenceRaw;
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, ref, part.rowsById);
  out[tab] = rows.filter(r=>r.session_id==='leaf').map(r=>({{orphan:!!r._orphan_child_session}}));
}}
console.log(JSON.stringify(out));
"""
    out = json.loads(_run_node(source))
    assert out["webui"] + out["cli"] == [{"orphan": True}], out


@pytest.mark.parametrize("parent_source", ["desktop", "webui"])
def test_5305_source_scoped_webui_payload_keeps_subagent_of_cli_tab_parent(monkeypatch, parent_source):
    """Re-gate: each tab is fetched alone (sidebar_source=webui|cli). An imported Desktop parent is
    CLI-tab (is_cli_session) so the WebUI payload holds only its child; the server stamps
    parent_is_cli_session so the child stays openable. A WebUI parent filtered out still suppresses."""
    import api.routes as routes

    desktop = parent_source == "desktop"
    parent = {
        "session_id": "p", "title": f"{parent_source.title()} session", "source": parent_source, "raw_source": parent_source,
        "source_tag": parent_source, "session_source": "other" if desktop else "webui",
        "is_cli_session": desktop, "profile": "default", "message_count": 5,
        "actual_message_count": 5, "actual_user_message_count": 2, "updated_at": 100, "last_message_at": 100,
    }
    if not desktop:
        parent["project_id"] = "elsewhere"
    child = {
        "session_id": "sub", "title": "Subagent Session", "parent_session_id": "p",
        "relationship_type": "child_session", "parent_source": parent_source, "source": "subagent",
        "raw_source": "subagent", "source_tag": "subagent", "session_source": "other",
        "profile": "default", "message_count": 3, "updated_at": 101, "last_message_at": 101,
    }
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [dict(parent), dict(child)])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _s: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **_k: rows)
    payload = routes._build_session_list_cache_payload(
        active_profile="default", all_profiles=False, show_cli_sessions=True,
        show_previous_messaging_sessions=False, show_cron_sessions=False, sidebar_source="webui",
    )
    rows = routes._session_list_payload_to_response(payload)["sessions"]
    if desktop:
        assert [r["session_id"] for r in rows] == ["sub"], rows
        assert rows[0]["parent_is_cli_session"] is True
    else:
        assert rows[next(i for i, r in enumerate(rows) if r["session_id"] == "sub")]["parent_is_cli_session"] is False
        rows = [r for r in rows if r["session_id"] == "sub"]
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._activeProject = null;
global._showArchived = false;
global.window = {{ _showCliSessions: true }};
global._sessionSourceFilter = 'webui';
const allMatched = {json.dumps(rows, default=str)};
const part = _partitionSidebarSessionRows(allMatched, null);
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw, part.rowsById);
console.log(JSON.stringify(rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session}}))));
"""
    out = json.loads(_run_node(source))
    assert out == ([{"sid": "sub", "orphan": True}] if desktop else []), out


@pytest.mark.parametrize("parent_source,expect", [("desktop", [{"sid": "sub", "orphan": True}]), ("cron", [])])
def test_5305_unstamped_child_infers_bucket_only_from_unambiguous_parent_source(parent_source, expect):
    """With no parent row and no parent_is_cli_session, an ambiguous source (desktop) keeps the
    child openable; only a source the server never files as CLI (cron) suppresses it."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._showArchived = false;
const raw = [
  {{ session_id:'sub', title:'Subagent Session', parent_session_id:'p', relationship_type:'child_session', parent_source:'{parent_source}', raw_source:'subagent', source_tag:'subagent', session_source:'other', message_count:3 }},
];
const rows = _attachChildSessionsToSidebarRows([], raw);
console.log(JSON.stringify(rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session}}))));
"""
    assert json.loads(_run_node(source)) == expect


def _render_tabs(rows_js, project=None):
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _preamble(js) + f"""
global._activeProject = {json.dumps(project)};
global._showArchived = false;
global.window = {{ _showCliSessions: true }};
global._sessionDisplayTitle = (s) => (s && s.title) || '';
const allMatched = {rows_js};
const out = {{}};
for (const tab of ['webui', 'cli']) {{
  global._sessionSourceFilter = tab;
  const part = _partitionSidebarSessionRows(allMatched, null);
  const ref = tab === 'cli' ? part.cliReferenceRaw : part.webuiReferenceRaw;
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, ref, part.rowsById);
  const flat = [];
  const walk = (r, orphan) => {{ flat.push({{sid:r.session_id, orphan}}); (r._child_sessions||[]).forEach(c=>walk(c, false)); }};
  rows.forEach(r=>walk(r, !!r._orphan_child_session));
  out[tab] = flat;
}}
console.log(JSON.stringify(out));
"""
    return json.loads(_run_node(source))


def _sub(sid, parent, parent_source, **extra):
    row = {"session_id": sid, "title": "Subagent Session", "parent_session_id": parent,
           "relationship_type": "child_session", "parent_source": parent_source, "raw_source": "subagent",
           "source_tag": "subagent", "session_source": "other", "profile": "a", "message_count": 3,
           "updated_at": 101, "last_message_at": 101}
    row.update(extra)
    return row


def _sids(tab):
    return [r["sid"] for r in tab]


def test_5305_nested_leaf_under_orphaned_subagent_of_cli_parent_stays_reachable():
    """D(CLI) -> O -> L: O orphans in the WebUI tab, so L must not be suppressed on the
    strength of O sharing its bucket; L is reachable in exactly one tab."""
    rows = [
        {"session_id": "D", "title": "CLI run", "session_source": "cli", "raw_source": "cli", "source_tag": "cli",
         "is_cli_session": True, "profile": "a", "message_count": 5, "updated_at": 100, "last_message_at": 100},
        _sub("O", "D", "cli"),
        _sub("L", "O", "subagent", updated_at=102),
    ]
    out = _render_tabs(json.dumps(rows))
    assert _sids(out["webui"]).count("L") + _sids(out["cli"]).count("L") == 1, out
    assert {"sid": "L", "orphan": True} in out["webui"], out


def test_5305_nested_chain_under_project_filtered_webui_parent_follows_parent():
    """W(project p, filtered out) -> O -> L: the whole chain follows W's scope (hidden in
    another project's view) and nests under W once W's project is shown."""
    w = {"session_id": "W", "title": "Parent", "session_source": "webui", "raw_source": "webui", "source_tag": "webui",
         "project_id": "p", "profile": "a", "message_count": 5, "updated_at": 100, "last_message_at": 100}
    rows = json.dumps([w, _sub("O", "W", "webui"), _sub("L", "O", "subagent", updated_at=102)])
    other = _render_tabs(rows, project="q")
    assert "L" not in _sids(other["webui"]) + _sids(other["cli"]), other
    own = _render_tabs(rows, project="p")
    assert own["webui"] == [{"sid": "W", "orphan": False}, {"sid": "O", "orphan": False}, {"sid": "L", "orphan": False}], own


def test_5305_leaf_of_compressed_subagent_under_visible_webui_parent_nests():
    """L names O's pre-compression segment (absent from the payload); it resolves through
    _parent_lineage_tip_id and nests under the visible chain instead of being hidden."""
    w = {"session_id": "W", "title": "Parent", "session_source": "webui", "raw_source": "webui", "source_tag": "webui",
         "profile": "a", "message_count": 5, "updated_at": 100, "last_message_at": 100}
    rows = [w, _sub("O_tip", "W", "webui"),
            _sub("L", "O_old", "subagent", updated_at=102, _parent_lineage_root_id="O_old", _parent_lineage_tip_id="O_tip")]
    out = _render_tabs(json.dumps(rows))
    assert _sids(out["webui"]).count("L") == 1 and "L" not in _sids(out["cli"]), out
    assert {"sid": "L", "orphan": False} in out["webui"], out


def _web(sid, **extra):
    row = {"session_id": sid, "title": sid, "session_source": "webui", "raw_source": "webui", "source_tag": "webui",
           "profile": "a", "message_count": 5, "updated_at": 100, "last_message_at": 100}
    row.update(extra)
    return row


def test_5305_leaf_of_missing_subagent_parent_orphans():
    """Re-gate #1: O (a subagent) is absent from the payload (e.g. zero-message, dropped by the
    route). A subagent parent is a child row, never a scope-hidden top-level host, so L orphans."""
    out = _render_tabs(json.dumps([_web("W"), _sub("L", "O", "subagent", updated_at=102)]))
    assert {"sid": "L", "orphan": True} in out["webui"], out
    assert "L" not in _sids(out["cli"]), out


def _compressed_subagent_under_cli_db(path):
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER, "
        "started_at REAL, source TEXT, parent_session_id TEXT, ended_at REAL, end_reason TEXT)"
    )
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
    rows = [
        ("W", "CLI run", 90.0, "cli", None, None, None),
        ("O_old", "Orchestrator", 100.0, "subagent", "W", 150.0, "compression"),
        ("O_tip", "Orchestrator", 151.0, "subagent", "O_old", None, None),
        ("L", "Leaf", 120.0, "subagent", "O_old", None, None),
    ]
    for sid, title, started, source, parent, ended, reason in rows:
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, title, "gpt", 2, started, source, parent, ended, reason),
        )
    for sid, t in {"W": 95.0, "O_old": 110.0, "O_tip": 160.0, "L": 900.0}.items():
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", (sid, "user", t))
    conn.commit()
    conn.close()


def test_5305_leaf_of_compressed_subagent_parent_resolves_in_all_profiles_payload(tmp_path):
    """Re-gate #2: importer-generated all-profiles rows (no lineage enrichment): L names O_old,
    the payload only has O_tip with _lineage_root_id='O_old' and L has no _parent_lineage_tip_id.
    L must resolve to O_tip's lineage instead of vanishing; like D(CLI) -> O -> L it orphans
    next to the orphaned O_tip in the WebUI tab."""
    import api.models as models

    db = tmp_path / "state.db"
    _compressed_subagent_under_cli_db(db)
    rows = models._load_cli_sessions_uncached(tmp_path, db, None, visible_session_limit=20, include_claude_code=False)
    by_id = {r["session_id"]: r for r in rows}
    assert set(by_id) == {"W", "O_tip", "L"}, sorted(by_id)
    assert by_id["O_tip"]["_lineage_root_id"] == "O_old"
    assert not by_id["L"].get("_parent_lineage_tip_id"), by_id["L"]
    for r in rows:
        r.setdefault("profile", "a")
    out = _render_tabs(json.dumps(rows, default=str))
    assert _sids(out["webui"]).count("L") + _sids(out["cli"]).count("L") == 1, out
    assert {"sid": "O_tip", "orphan": True} in out["webui"], out
    assert {"sid": "L", "orphan": True} in out["webui"], out


def test_5305_subagent_of_orphaned_branch_child_stays_reachable():
    """Re-gate #3: W in project q, B a (non-subagent) child of W with no project, L a subagent
    of B, viewing Unassigned. B renders as an orphan; L must not be hidden behind it."""
    rows = [
        _web("W", project_id="q"),
        _web("B", parent_session_id="W", relationship_type="child_session", parent_source="webui"),
        _sub("L", "B", "webui", updated_at=102),
    ]
    out = _render_tabs(json.dumps(rows), project="__no_project__")
    assert {"sid": "B", "orphan": True} in out["webui"], out
    assert "L" in _sids(out["webui"]), out


@pytest.mark.parametrize("tip_hint", [False, True])
def test_5305_leaf_of_compressed_subagent_follows_project_of_lineage(tip_hint):
    """Re-gate (5443895388): W(project p) -> O_old -> O_tip, L.parent_session_id='O_old', all
    profiles. Project filtering must resolve O_old through lineage like attachment does: L
    nests under W in project p and stays out of Unassigned (master: L listed in Unassigned)."""
    extra = {"_parent_lineage_root_id": "O_old"}
    if tip_hint:
        extra["_parent_lineage_tip_id"] = "O_tip"
    rows = json.dumps([
        _web("W", project_id="p"),
        _sub("O_tip", "W", "webui", _lineage_root_id="O_old"),
        _sub("L", "O_old", "subagent", updated_at=102, **extra),
    ])
    own = _render_tabs(rows, project="p")
    assert own["webui"] == [{"sid": "W", "orphan": False}, {"sid": "O_tip", "orphan": False},
                            {"sid": "L", "orphan": False}], own
    unassigned = _render_tabs(rows, project="__no_project__")
    assert "L" not in _sids(unassigned["webui"]) + _sids(unassigned["cli"]), unassigned


def test_5305_subagent_of_sidecarless_webui_parent_outside_slice_stays_reachable(tmp_path):
    """Greptile 4205554465: a webui-source parent in the oversample but not the 20-row slice (and
    with no WebUI sidecar) is not returned, so the child must not keep parent_source='webui'
    ("parent is in this payload"); otherwise the client hides it in both tabs."""
    import api.models as models

    db = tmp_path / "state.db"
    _desktop_parent_db(db, newer=19, parent_source="webui")
    rows = models._load_cli_sessions_uncached(tmp_path, db, None, visible_session_limit=20, include_claude_code=False)
    ids = [r["session_id"] for r in rows]
    assert "leaf" in ids and "desk" not in ids, ids
    assert next(r for r in rows if r["session_id"] == "leaf")["parent_source"] is None
    for r in rows:
        r.setdefault("profile", "a")
    out = _render_tabs(json.dumps(rows, default=str))
    assert [r for r in out["webui"] + out["cli"] if r["sid"] == "leaf"] == [{"sid": "leaf", "orphan": True}], out
