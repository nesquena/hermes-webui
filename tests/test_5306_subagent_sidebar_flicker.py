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
  const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, ref);
  out[tab] = rows.map(r=>({{sid:r.session_id, orphan:!!r._orphan_child_session, kids:(r._child_sessions||[]).map(c=>c.session_id)}}));
}}
console.log(JSON.stringify(out));
"""
    out = json.loads(_run_node(source))
    assert out["webui"] == [{"sid": "sub", "orphan": True, "kids": []}]
    assert out["cli"] == [{"sid": "cli_parent", "orphan": False, "kids": []}]


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
const rows = _renderSidebarRowsFromRawSessions(part.sessionsRaw, part.webuiReferenceRaw);
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
